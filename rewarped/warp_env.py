import numpy as np
import torch
from gym import spaces

import warp as wp

from .autograd import UpdateFunction
from .environment import Environment, RenderMode
from .warp.model_monkeypatch import Model_control, Model_state


@torch.jit.script
def scatter_clone(input, index, src, dim: int = -1):
    """Write `src` into a copy of `input` at indices specified in `index` along axis `dim`.

    Gradients will flow back from the result of this operation to `src`, but not `input`.
    """
    input = input.detach().clone()
    if src.requires_grad:
        input.requires_grad_()
    out = input.scatter(dim, index, src)
    return out


class StateTensors:
    r"""Simple wrapper around `wp.sim.State()` to access `state_tensors` as attributes.

    Note that `state_tensors` are returned by `torch.autograd.Function.apply`, since
    currently `wp.to_torch(wp.from_torch(state_tensor)))` breaks the compute graph.
    """

    def __init__(self, state, state_tensors_names, state_tensors):
        self.state = state
        self.state_tensors_names = state_tensors_names
        self.state_tensors = state_tensors

    def assign(self, name, tensor):
        if self.state_tensors is not None and name in self.state_tensors_names:
            idx = self.state_tensors_names.index(name)
            self.state_tensors[idx] = tensor
            # setattr(self.state, name, wp.from_torch(tensor, dtype=getattr(self.state, name).dtype))
            getattr(self.state, name).assign(wp.from_torch(tensor))
        else:
            getattr(self.state, name).assign(wp.from_torch(tensor))

    def __getattr__(self, name):
        if self.state_tensors is not None and name in self.state_tensors_names:
            idx = self.state_tensors_names.index(name)
            return self.state_tensors[idx]
        return wp.to_torch(getattr(self.state, name))


class ControlTensors:
    def __init__(self, control, control_tensors_names, control_tensors):
        self.control = control
        self.control_tensors_names = control_tensors_names
        self.control_tensors = control_tensors

    def assign(self, name, tensor):
        if self.control_tensors is not None and name in self.control_tensors_names:
            idx = self.control_tensors_names.index(name)
            self.control_tensors[idx] = tensor
            # setattr(self.control, name, wp.from_torch(tensor, dtype=getattr(self.control, name).dtype))
            getattr(self.control, name).assign(wp.from_torch(tensor))
        else:
            getattr(self.control, name).assign(wp.from_torch(tensor))

    def __getattr__(self, name):
        if self.control_tensors is not None and name in self.control_tensors_names:
            idx = self.control_tensors_names.index(name)
            return self.control_tensors[idx]
        return wp.to_torch(getattr(self.control, name))


class WarpEnv(Environment):
    r"""Base class for gym-like Warp environments that builds on `Environment`.

    Control flow:
    ```
    WarpEnv.reset() ->
      if (not initialized):
        WarpEnv.init() ->
          Environment.init() ->
            Environment.create_env() ->
              Environment.create_articulation()  # create objects in the scene
          WarpEnv.init_sim()
      WarpEnv.reset_idx()
        -> WarpEnv.randomize_init()
      WarpEnv.compute_observations()

    WarpEnv.step() ->
      WarpEnv.pre_physics_step()  # assign actions to self.control
      WarpEnv.do_physics_step() ->
        if (requires_grad) or (use_graph_capture):
          UpdateFunction() ->
            sim_update() ->
              Integrator.simulate()
        else:
          Environment.update() ->  # should behave like sim_update()
            Integrator.simulate()

      WarpEnv.compute_observations()
      WarpEnv.compute_reward()

      if (env_ids):
        WarpEnv.reset(env_idx)
      WarpEnv.render()
    ```
    """

    state_tensors_names = ()
    control_tensors_names = ()

    def __init__(
        self,
        num_envs: int,
        num_obs,
        num_act,
        episode_length: int,
        early_termination=False,
        randomize=True,
        seed=0,
        no_grad=False,
        render=False,
        render_mode="usd",
        device="cuda:0",
        use_graph_capture=True,
        synchronize=False,
        max_unroll=16,
        debug=False,
    ):
        super().__init__()
        # Environment parameters
        self.visualize = render
        if render_mode is not None:
            self.render_mode = RenderMode(render_mode) if render else RenderMode.NONE
        # if self.render_mode == RenderMode.NONE:
        #     self.env_offset = (0.0, 0.0, 0.0)  # set to zero for training for numerical consistency
        self.num_envs = num_envs
        self.no_grad = no_grad
        self.device = device
        self.use_graph_capture = use_graph_capture and "cuda" in device
        self.synchronize = synchronize

        if debug:
            import faulthandler

            faulthandler.enable()

            options = {
                "verify_fp": True,
                "verify_cuda": True,
                "print_launches": True,
                "mode": "debug",
                "verbose": True,
                "verbose_warnings": True,
                # "kernel_cache_dir": None,  # TODO: expects str
                "enable_backward": self.requires_grad,
                "max_unroll": max_unroll,
            }
            # Make sure Warp was built with `build_lib.py --mode=debug`
            assert wp.context.runtime.core.is_debug_enabled()
        else:
            options = {
                "verify_fp": False,
                # "kernel_cache_dir": None,  # TODO: expects str
                "enable_backward": self.requires_grad,
                "max_unroll": max_unroll,
            }
        for k, v in options.items():
            setattr(wp.config, k, v)
        print("Warp options:", wp.get_module_options())

        # WarpEnv parameters
        self.episode_length = episode_length
        self.early_termination = early_termination
        self.seed = seed
        self.randomize = randomize
        self.initialized = False

        self.num_obs = num_obs
        self.num_act = num_act
        self.obs_space = spaces.Box(np.ones(self.num_obs) * -np.Inf, np.ones(self.num_obs) * np.Inf)
        self.act_space = spaces.Box(np.ones(self.num_act) * -1.0, np.ones(self.num_act) * 1.0)

        self.scatter_actions = staticmethod(scatter_clone)  # alias for convenience

    @property
    def num_observations(self):
        return self.num_obs

    @property
    def num_actions(self):
        return self.num_act

    @property
    def observation_space(self):
        return self.obs_space

    @property
    def action_space(self):
        return self.act_space

    @property
    def max_episode_steps(self):
        return self.episode_length

    @property
    def max_episode_length(self):
        return self.episode_length

    @property
    def state(self):
        return StateTensors(self.state_0, self.state_tensors_names, self.state_tensors)

    @property
    def control(self):
        return ControlTensors(self.control_0, self.control_tensors_names, self.control_tensors)

    @property
    def requires_grad(self):
        return not self.no_grad

    def init(self):
        # ---- Init simulation
        wp.set_device(self.device)
        super().init()
        self.init_sim()

        # Allocate buffers
        self.obs_buf = torch.zeros(
            (self.num_envs, self.num_obs),
            dtype=torch.float,
            device=self.device,
        )
        self.actions = torch.zeros(
            (self.num_envs, self.num_act),
            dtype=torch.float,
            device=self.device,
            requires_grad=self.requires_grad,
        )
        self.rew_buf = torch.zeros(
            self.num_envs,
            dtype=torch.float,
            device=self.device,
        )
        self.reset_buf = torch.ones(self.num_envs, device=self.device, dtype=torch.bool, requires_grad=False)
        self.terminated_buf = torch.zeros(self.num_envs, device=self.device, dtype=torch.bool, requires_grad=False)
        self.truncated_buf = torch.ones(self.num_envs, device=self.device, dtype=torch.bool, requires_grad=False)
        self.progress_buf = torch.zeros(self.num_envs, device=self.device, dtype=torch.int, requires_grad=False)
        self.extras = {}

    def create_model(self):
        model = super().create_model()

        def model_state_fn(model, *args, **kwargs):
            return Model_state(model, *args, integrator_type=self.integrator_type.value, **kwargs)

        # monkeypatch model.state() function
        model.state = model_state_fn.__get__(model, model.__class__)

        # monkeypatch model.control() function
        model.control = Model_control.__get__(model, model.__class__)

        return model

    def init_sim(self):
        self.sim_time = 0.0
        self.render_time = 0.0
        self.num_frames = 0

        self._env_offsets = self.env_offsets
        self.env_offsets = torch.tensor(self.env_offsets, dtype=torch.float, device=self.device)

        self.state_0 = self.model.state()
        self.state_1 = self.model.state(copy="zeros")
        self.states_mid = None
        self.control_0 = self.model.control()

        if self.eval_fk:
            wp.sim.eval_fk(self.model, self.state_0.joint_q, self.state_0.joint_qd, None, self.state_0)

        self.tape = None
        if self.requires_grad or self.use_graph_capture:
            if self.requires_grad:
                assert self.model.requires_grad

                self.state_tensors = [wp.to_torch(getattr(self.state_0, k)) for k in self.state_tensors_names]
                self.control_tensors = [wp.to_torch(getattr(self.control_0, k)) for k in self.control_tensors_names]
            else:
                self.state_tensors_names, self.control_tensors_names = [], []
                self.state_tensors = []
                self.control_tensors = []

            if self.use_graph_capture:
                self.tape = wp.Tape()  # persistent tape for graph capture

                self.state_0_bwd = self.model.state(copy="zeros")
                self.state_1_bwd = self.model.state(copy="zeros")
                self.states_mid_bwd = [self.model.state(copy="zeros") for _ in range(self.sim_substeps - 1)]
                self.control_0_bwd = self.model.control()

                # graph capture done inside first call to UpdateFunction.apply()
                self.integrator.update_graph = None
                self.integrator.bwd_update_graph = None
        else:
            self.integrator.update_graph = None
            self.state_tensors = None
            self.control_tensors = None
        print(f"grads: {self.requires_grad}, graph_capture: {self.use_graph_capture}, synchronize: {self.synchronize}")

    def initialize_trajectory(self):
        """
        This function starts collecting a new trajectory from the current states,
        but cuts off the computation graph to the previous states.
        It has to be called every time the algorithm starts an episode and it returns the observation vectors
        """
        self.clear_grad()
        self.compute_observations()
        return self.obs_buf

    def clear_grad(self, checkpoint=None):
        """
        cut off the gradient from the current state to previous states
        """
        with torch.no_grad():
            if checkpoint is None:
                checkpoint = self.get_checkpoint(detach=True)

        self.actions = self.actions.detach().clone()
        self.rew_buf = self.rew_buf.detach().clone()
        self.reset_buf = self.reset_buf.detach().clone()
        self.terminated_buf = self.terminated_buf.detach().clone()
        self.truncated_buf = self.truncated_buf.detach().clone()
        self.progress_buf = self.progress_buf.detach().clone()

        for k, v in checkpoint["state"].items():
            self.state.assign(k, v)
        for k, v in checkpoint["control"].items():
            self.control.assign(k, v)

    def get_checkpoint(self, detach=False):
        if not self.initialized:
            raise RuntimeError("WarpEnv is not initialized, call reset() first")

        checkpoint = {"state": {}, "control": {}}
        for k in self.state_tensors_names:
            v = getattr(self.state, k)
            if detach:
                v = v.detach()
            checkpoint["state"][k] = v.clone()
        for k in self.control_tensors_names:
            v = getattr(self.control, k)
            if detach:
                v = v.detach()
            checkpoint["control"][k] = v.clone()
        return checkpoint

    def reset(self, env_ids=None, clear_grad=None):
        if not self.initialized:
            self.init()
            self.initialized = True

        if env_ids is None:
            env_ids = torch.arange(self.num_envs, device=self.device)

        self.reset_idx(env_ids)

        # clear action
        N = len(env_ids)
        if N == self.num_envs:
            self.actions = torch.zeros((self.num_envs, self.num_actions), dtype=torch.float, device=self.device)
        else:
            if self.requires_grad and clear_grad is False:
                new_actions = torch.zeros((self.num_envs, self.num_actions), dtype=torch.float, device=self.device)
                new_actions.requires_grad_()
                mask = torch.ones(self.num_envs, dtype=torch.bool, device=self.device)
                mask[env_ids] = 0  # mask == True if not reset
                other_env_ids = torch.where(mask)[0]
                if len(other_env_ids) > 0:
                    indices = other_env_ids.unsqueeze(1).expand(-1, self.num_actions)
                    new_actions.scatter(0, indices, self.actions)
                self.actions = new_actions
            else:
                self.actions = self.actions.detach().clone()
                self.actions[env_ids, :] = torch.zeros((N, self.num_actions), dtype=torch.float, device=self.device)

        self.progress_buf[env_ids] = 0
        self.num_frames = 0

        self.integrator._step = 0

        # initialize_trajectory()
        if self.requires_grad and clear_grad:
            self.clear_grad()
        self.compute_observations()

        return self.obs_buf

    def do_physics_step(self):
        if self.requires_grad or self.use_graph_capture:
            tape = self.tape
            update_params = (tape, self.integrator, self.model, self.use_graph_capture, self.synchronize)
            sim_params = (self.sim_substeps, self.sim_dt, self.kinematic_fk, self.eval_ik)

            if self.requires_grad:
                state_1 = self.model.state(copy="zeros")  # TODO: could cache these if optim window is known
            else:
                state_1 = self.state_1

            if self.use_graph_capture:
                states_mid = self.states_mid

                assert tape is not None
                states_bwd = (self.state_0_bwd, self.states_mid_bwd, self.state_1_bwd)
                control_bwd = self.control_0_bwd
            else:
                # states_mid = self.states_mid
                states_mid = [self.model.state(copy="zeros") for _ in range(self.sim_substeps - 1)]

                states_bwd = (None, None, None)
                control_bwd = None

            states = (self.state_0, states_mid, state_1)
            control = self.control_0

            if self.requires_grad:
                tensors = tuple(self.state_tensors + self.control_tensors)
                outputs = UpdateFunction.apply(
                    update_params,
                    sim_params,
                    states,
                    control,
                    states_bwd,
                    control_bwd,
                    self.state_tensors_names,
                    self.control_tensors_names,
                    *tensors,
                )
            else:
                ctx = torch.autograd.function.FunctionCtx()
                state_tensors_names, control_tensors_names = [], []
                outputs = UpdateFunction.forward(
                    ctx,
                    update_params,
                    sim_params,
                    states,
                    control,
                    states_bwd,
                    control_bwd,
                    state_tensors_names,
                    control_tensors_names,
                )
                outputs = []

            num_state = len(self.state_tensors_names)
            self.state_tensors = list(outputs[:num_state])
            self.state_1 = self.state_0  # needed for renderer
            self.state_0 = state_1

            self.control_0 = self.model.control()
            self.control_tensors = [wp.to_torch(getattr(self.control_0, k)) for k in self.control_tensors_names]
        else:
            super().update()

    def step(self, actions):
        with wp.ScopedTimer("simulate", active=False, detailed=False):
            self.pre_physics_step(actions)
            self.do_physics_step()
            self.sim_time += self.frame_dt

        self.progress_buf += 1
        self.num_frames += 1
        self.reset_buf = torch.zeros_like(self.reset_buf)

        # post_physics_step()
        self.compute_observations()
        self.compute_reward()
        self.extras = {
            "terminated": self.terminated_buf.clone(),
            "truncated": self.truncated_buf.clone(),
            "obs_before_reset": None,
        }

        env_ids = self.reset_buf.nonzero(as_tuple=False).squeeze(-1)
        if len(env_ids) > 0:
            if isinstance(self.obs_buf, dict):
                obs_buf_before_reset = {k: v.clone() for k, v in self.obs_buf.items()}
            else:
                obs_buf_before_reset = self.obs_buf.clone()
            self.extras["obs_before_reset"] = obs_buf_before_reset

            with wp.ScopedTimer("reset", active=False, detailed=False):
                self.reset(env_ids)

        # NOTE: this occurs post reset, so will render initial state (not terminal state)
        with wp.ScopedTimer("render", active=False, detailed=False):
            self.render()

        return self.obs_buf, self.rew_buf, self.reset_buf, self.extras

    def reset_idx(self, env_ids):
        assert len(env_ids) == self.num_envs
        assert not self.early_termination
        # reset state and control
        self.state_0 = self.model.state()
        self.control_0 = self.model.control()
        self.state_0.clear_forces()
        self.control_0.clear_acts()

        if self.randomize:
            self.randomize_init(env_ids)

        if self.eval_fk:
            mask = None
            wp.sim.eval_fk(self.model, self.state_0.joint_q, self.state_0.joint_qd, mask, self.state_0)
            # TODO: wrap this in torch.autograd.Function?

        self.state_tensors = [wp.to_torch(getattr(self.state_0, k)) for k in self.state_tensors_names]
        self.control_tensors = [wp.to_torch(getattr(self.control_0, k)) for k in self.control_tensors_names]

    def randomize_init(self, env_ids):
        raise NotImplementedError

    def pre_physics_step(self, actions):
        """Apply the actions to the environment (eg by setting torques, position targets)."""
        raise NotImplementedError

    def compute_observations(self):
        raise NotImplementedError

    def compute_reward(self):
        raise NotImplementedError

    def run(self, N=2):
        # return super().run()

        # torch.autograd.set_detect_anomaly(True)

        for n in range(N):
            obs = self.reset()

            profiler = {}
            with wp.ScopedTimer("episode", detailed=False, print=False, active=True, dict=profiler):
                obses, actions, rewards, dones, infos = [obs], [], [], [], []
                for i in range(self.episode_length):
                    action_shape = (self.num_envs, self.num_actions)
                    action = torch.randn(action_shape, device=self.device, requires_grad=self.requires_grad) * 2.0 - 1.0
                    # action[...] = 1.0
                    obs, reward, done, info = self.step(action)

                    print(i, "/", self.episode_length)
                    # print("action:", action.detach().cpu().numpy().flatten())

                    obses.append(obs)
                    actions.append(action)
                    rewards.append(reward)
                    dones.append(done)
                    infos.append(info)

                if self.synchronize:
                    wp.synchronize_device()

                # loss = -torch.stack(rewards).sum()
                # loss.backward()

            avg_time = np.array(profiler["episode"]).mean() / self.episode_length
            avg_steps_second = 1000.0 * float(self.num_envs) / avg_time
            total_time_second = np.array(profiler["episode"]).sum() / 1000.0

            print(
                f"num_envs: {self.num_envs} |",
                f"steps/second: {avg_steps_second:.4} |",
                f"milliseconds/step: {avg_time:.4f} |",
                f"total_seconds: {total_time_second:.4f} |",
            )

        if self.renderer is not None:
            self.renderer.save()

        return 1000.0 * float(self.num_envs) / avg_time
