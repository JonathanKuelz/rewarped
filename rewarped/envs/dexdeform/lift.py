from ...environment import run_env
from .hand import Hand


class Lift(Hand):
    sim_name = "Lift" + "DexDeform"

    TRAJ_OPT = True
    LOAD_DEMO = False

    def __init__(self, task_name="lift", **kwargs):
        super().__init__(task_name=task_name, **kwargs)


if __name__ == "__main__":
    run_env(Lift, no_grad=False)
