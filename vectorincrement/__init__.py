from vectorincrement.vectorincrementenv import VectorIncrementEnvironment
from observation_encoder import ObservationScaleWrapper
import gym
from gym.wrappers import TimeLimit
import gin


gym.envs.register(
     id='VectorIncrement-v0',
     entry_point=VectorIncrementEnvironment,
)

@gin.configurable
def load_env(env_name, time_limit=None, obs_scaler=None, wrappers=None, **kwargs):
    """Load an environment, configurable via gin."""
    print(f"Make environment {env_name} {wrappers} {kwargs}")
    env = gym.make(env_name, **kwargs)
    if time_limit:
        env = TimeLimit(env, time_limit)
    if obs_scaler:
        env = ObservationScaleWrapper(env, obs_scaler)
    if wrappers is None:
        wrappers = []
    for wrapper in wrappers[::-1]:
        env = wrapper(env)
    return env
