from time import time
import os
os.environ['CUDA_VISIBLE_DEVICES'] = "-1"
import torch
import gym
import numpy as np
import pytest
import gin

from keychest.keychestenv import KeyChestGymEnv, KeyChestEnvironmentRandom, KeyChestEnvironmentFixedMap
from keychest.keychestenv import split_images, unsplit_images_np
from keychest.features_xy import dict_to_arr, arr_to_dict, obs_features_handcoded, reconstruct_image_from_features
import keychest.keychestenv as kcenv_module
from .gofa_model import manual_model_features

def test_hardcoded_env_behavior():
    def random_reward():
        return np.random.rand() * 5 - 3

    mymap = """

P<>
l@ 
B  



"""

    mymap2 = ["P<>", "l@ ", "B  "]

    mymap3 = np.array([['P', '<', '>'],
                       ['l', '@', ' '],
                       ['B', ' ', ' ']],
                      dtype='<U1')

    maps = [mymap, mymap2, mymap3]

    for map_ in maps:
        reward = {'step': -1, 'food_collected': random_reward(), 'key_collected': random_reward(),
                  'chest_opened': random_reward()}

        env = KeyChestGymEnv(engine_constructor=KeyChestEnvironmentFixedMap,
                             map_array=map_, initial_health=9, food_efficiency=3,
                             reward_dict=reward)
        obs = env.reset()

        obs_hardcoded_match = np.array([['@', '@', '@', '@', '@'],
                                        ['@', '@', '@', '@', ' '],
                                        [' ', ' ', ' ', ' ', ' '],
                                        [' ', ' ', ' ', ' ', ' '],
                                        ['#', '#', '#', '#', '#'],
                                        ['#', 'P', '<', '>', '#'],
                                        ['#', 'l', '@', ' ', '#'],
                                        ['#', 'B', ' ', ' ', '#'],
                                        ['#', '#', '#', '#', '#']], dtype='<U1')

        def assert_obs_equals(env, obs):
            print(env.render(mode='np_array').shape, obs.shape)
            print(np.where(env.render(mode='np_array') != obs))
            print(env.render(mode='np_array'))
            assert all((env.render(mode='np_array') == obs).flatten()), "Wrong observation"

        assert env.engine.shape == (3, 3)
        assert env.engine.health == 9
        assert env.engine.keys == 0
        assert env.engine.player_position == (0, 0)
        assert env.engine.lamp_state == 0
        assert_obs_equals(env, obs_hardcoded_match)

        assert isinstance(env.render('rgb_array'), np.ndarray)

        obs, rew, done, info = env.step_string('up')
        assert env.engine.player_position == (0, 0)
        assert env.engine.health == 8
        assert rew == -1
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('right')
        assert env.engine.health == 7
        assert env.engine.keys == 0
        assert env.engine.player_position == (0, 1)
        assert rew == -1
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('right')
        assert env.engine.health == 6
        assert env.engine.keys == 1
        assert env.engine.player_position == (0, 2)
        assert rew == -1 + reward['key_collected']
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('right')
        assert env.engine.health == 5
        assert env.engine.keys == 0
        assert env.engine.player_position == (0, 2)
        assert rew == -1 + reward['chest_opened']
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('down')
        assert env.engine.health == 4
        assert env.engine.keys == 0
        assert env.engine.player_position == (1, 2)
        assert rew == -1
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('left')
        assert env.engine.health == 3
        assert env.engine.keys == 0
        assert env.engine.player_position == (1, 1)
        assert rew == -1
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('left')
        assert env.engine.health == 2 + env.engine.food_efficiency
        assert env.engine.keys == 0
        assert env.engine.player_position == (1, 0)
        assert rew == -1 + reward['food_collected']
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('down')
        assert env.engine.health == 1 + env.engine.food_efficiency
        assert env.engine.keys == 0
        assert env.engine.player_position == (2, 0)
        assert rew == -1
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('down')
        assert env.engine.health == 3
        assert env.engine.keys == 0
        assert env.engine.player_position == (2, 0)
        assert rew == -1
        assert env.engine.lamp_state == 1
        assert done == False

        obs, rew, done, info = env.step_string('right')
        assert env.engine.health == 2
        assert env.engine.keys == 0
        assert env.engine.player_position == (2, 1)
        assert rew == -1
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('right')
        assert env.engine.health == 1
        assert env.engine.keys == 0
        assert env.engine.player_position == (2, 2)
        assert rew == -1
        assert env.engine.lamp_state == 0
        assert done == False

        obs, rew, done, info = env.step_string('right')
        assert env.engine.health == 0
        assert env.engine.keys == 0
        assert env.engine.player_position == (2, 2)
        assert rew == -1
        assert env.engine.lamp_state == 0
        assert done == True


def test_env_create():
    env = KeyChestGymEnv(engine_constructor=KeyChestEnvironmentRandom,
                         initial_health=15, food_efficiency=10)
    assert isinstance(env.observation_space, gym.spaces.Box)
    assert isinstance(env.action_space, gym.spaces.Discrete)
    assert env


def test_rollouts(do_print=False, time_for_test=3):
    """Do rollouts and see if the environment crashes."""
    time_start = time()

    while True:
        if time() - time_start > time_for_test:
            break

        # obtaining random params
        width = np.random.choice(np.arange(1, 20))
        height = np.random.choice(np.arange(1, 20))
        n_keys = np.random.choice(np.arange(1, 20))
        n_chests = np.random.choice(np.arange(1, 20))
        n_food = np.random.choice(np.arange(1, 20))
        initial_health = np.random.choice(np.arange(1, 20))
        food_efficiency = np.random.choice(np.arange(1, 20))

        wh = width * height
        n_objects = 3 + n_keys + n_chests + n_food

        params = dict(width=width, height=height, n_keys=n_keys, n_chests=n_chests, n_food=n_food,
                      initial_health=initial_health, food_efficiency=food_efficiency)

        if do_print:
            print("Obtained params", params)

        if n_objects > wh:
            with pytest.raises(AssertionError) as excinfo:
                # creating environment
                KeyChestGymEnv(engine_constructor=KeyChestEnvironmentRandom,
                               **params)
            assert str(excinfo.value).startswith('Too small width*height')
            continue
        else:
            env = KeyChestGymEnv(engine_constructor=KeyChestEnvironmentRandom,
                                 **params)

        assert isinstance(env, KeyChestGymEnv)

        # doing episodes
        for episode in range(20):
            obs = env.reset()
            img = env.render(mode='rgb_array')
            assert img.shape[2] == 3
            done = False
            steps = 0

            while not done:
                act = env.action_space.sample()
                obs, rew, done, info = env.step(act)
                img = env.render(mode='rgb_array')
                assert img.shape[2] == 3
                steps += 1


def test_wrong_action():
    env = KeyChestGymEnv(engine_constructor=KeyChestEnvironmentRandom,
                         initial_health=15, food_efficiency=10)
    with pytest.raises(KeyError) as excinfo:
        env.step(222)
    assert str(excinfo.value) == '222'


def test_image_split_unsplit():
    gin.bind_parameter('KeyChestEnvironment.flatten_observation', False)
    gin.bind_parameter('KeyChestEnvironment.return_rgb', True)
    gin.bind_parameter('obss_to_rgb.ignore_empty', True)

    env = KeyChestGymEnv(engine_constructor=KeyChestEnvironmentRandom,
                         initial_health=15, food_efficiency=10)
    obss = torch.from_numpy(np.array([env.reset(), env.reset(), env.reset()]))
    top, bot = split_images(env.engine, obss)
    obss_unsplit = unsplit_images_np(env.engine, top.numpy(), bot.numpy())
    assert np.allclose(obss_unsplit, obss.numpy())

    gin.clear_config()

def test_features_xy():
    gin.bind_parameter('KeyChestEnvironment.flatten_observation', False)
    gin.bind_parameter('KeyChestEnvironment.return_rgb', False)

    env = KeyChestGymEnv(engine_constructor=KeyChestEnvironmentRandom,
                         initial_health=15, food_efficiency=10)


    obss = []
    for _ in range(10):
        obss.append(env.reset())
        done = False
        while not done:
            obs, rew, done, info = env.step(env.action_space.sample())
            obss.append(obs)

    features_dicts = [obs_features_handcoded(obs=obs, engine=env.engine) for obs in obss]
    features_vect = [dict_to_arr(f) for f in features_dicts]
    features_dicts_rec = [arr_to_dict(arr=x, keys=features_dicts[0].keys()) for x in features_vect]
    obss_reconstruct = [reconstruct_image_from_features(env.engine, f) for f in features_dicts_rec]

    print(len(obss_reconstruct))

    assert len(obss_reconstruct) == len(obss)
    assert len(obss) > 0

    for o1, o2 in zip(obss, obss_reconstruct):
        assert np.allclose(o1, o2)

    gin.clear_config()

def test_gofa_model():
    gin.parse_config_file(os.path.join(os.path.dirname(kcenv_module.__file__), 'config', '5x5_1f1c1k.gin'))
    gin.bind_parameter('KeyChestEnvironment.flatten_observation', False)
    gin.bind_parameter('KeyChestEnvironment.return_rgb', False)
    gin.bind_parameter('KeyChestEnvironment.return_features_xy', False)


    env = KeyChestGymEnv()

    obs_x = []
    obs_y = []
    act_x = []
    for _ in range(10):
        obs_x.append(env.reset())
        done = False
        while not done:
            a = env.action_space.sample()
            obs, rew, done, info = env.step(a)
            a_1hot = np.zeros(4)
            a_1hot[a] = 1
            act_x.append(a_1hot)
            if not done:
                obs_x.append(obs)
            obs_y.append(obs)

    f_x = [obs_features_handcoded(obs=obs, engine=env.engine) for obs in obs_x]
    f_y = [obs_features_handcoded(obs=obs, engine=env.engine) for obs in obs_y]

    f_t1 = [manual_model_features(f, a, env.engine) for f, a in zip(f_x, act_x)]

    keys_differ = {}
    for ft1_correct, ft1 in zip(f_y, f_t1):
        for key in f_x[0].keys():
            if ft1_correct[key] != ft1[key]:
                if key not in keys_differ:
                    keys_differ[key] = []
                if len(keys_differ[key]) < 10:
                    keys_differ[key].append({'correct': ft1_correct[key],
                                             'given': ft1[key]})

    assert not keys_differ, keys_differ

    gin.clear_config()
