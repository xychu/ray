import unittest
import ray
from ray.rllib.algorithms.dqn import DQNConfig
from ray.rllib.offline.estimators import (
    ImportanceSampling,
    WeightedImportanceSampling,
    DirectMethod,
    DoublyRobust,
)
from ray.rllib.offline.estimators.fqe_torch_model import FQETorchModel
from ray.rllib.offline.json_reader import JsonReader
from ray.rllib.policy.sample_batch import concat_samples
from pathlib import Path
import os
import numpy as np
import gym


class TestOPE(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ray.init()
        rllib_dir = Path(__file__).parent.parent.parent.parent
        train_data = os.path.join(rllib_dir, "tests/data/cartpole/large.json")
        eval_data = train_data

        env_name = "CartPole-v0"
        cls.gamma = 0.99
        n_episodes = 40
        cls.q_model_config = {"n_iters": 600}

        config = (
            DQNConfig()
            .environment(env=env_name)
            .training(gamma=cls.gamma)
            .rollouts(num_rollout_workers=3, batch_mode="complete_episodes")
            .framework("torch")
            .resources(num_gpus=int(os.environ.get("RLLIB_NUM_GPUS", 0)))
            .offline_data(input_=train_data)
            .evaluation(
                evaluation_interval=None,
                evaluation_duration=n_episodes,
                evaluation_num_workers=1,
                evaluation_duration_unit="episodes",
                evaluation_config={"input": eval_data},
                off_policy_estimation_methods={
                    "is": {"type": ImportanceSampling},
                    "wis": {"type": WeightedImportanceSampling},
                    "dm_fqe": {
                        "type": DirectMethod,
                        "q_model_config": {"type": FQETorchModel},
                    },
                    "dr_fqe": {
                        "type": DoublyRobust,
                        "q_model_config": {"type": FQETorchModel},
                    },
                },
            )
        )
        cls.algo = config.build()

        # Train DQN for evaluation policy
        for _ in range(n_episodes):
            cls.algo.train()

        # Read n_episodes of data, assuming that one line is one episode
        reader = JsonReader(eval_data)
        cls.batch = reader.next()
        for _ in range(n_episodes - 1):
            cls.batch = concat_samples([cls.batch, reader.next()])
        cls.n_episodes = len(cls.batch.split_by_episode())
        print("Episodes:", cls.n_episodes, "Steps:", cls.batch.count)

        cls.mean_ret = {}
        cls.std_ret = {}
        cls.losses = {}

        # Simulate Monte-Carlo rollouts
        mc_ret = []
        env = gym.make(env_name)
        for _ in range(n_episodes):
            obs = env.reset()
            done = False
            rewards = []
            while not done:
                act = cls.algo.compute_single_action(obs)
                obs, reward, done, _ = env.step(act)
                rewards.append(reward)
            ret = 0
            for r in reversed(rewards):
                ret = r + cls.gamma * ret
            mc_ret.append(ret)

        cls.mean_ret["simulation"] = np.mean(mc_ret)
        cls.std_ret["simulation"] = np.std(mc_ret)

    @classmethod
    def tearDownClass(cls):
        print("Standalone OPE results")
        print("Mean:")
        print(*list(cls.mean_ret.items()), sep="\n")
        print("Stddev:")
        print(*list(cls.std_ret.items()), sep="\n")
        print("Losses:")
        print(*list(cls.losses.items()), sep="\n")
        ray.shutdown()

    def test_is(self):
        name = "is"
        estimator = ImportanceSampling(
            policy=self.algo.get_policy(),
            gamma=self.gamma,
        )
        estimates = estimator.estimate(self.batch)
        self.mean_ret[name] = estimates["v_target"]
        self.std_ret[name] = estimates["v_target_std"]

    def test_wis(self):
        name = "wis"
        estimator = WeightedImportanceSampling(
            policy=self.algo.get_policy(),
            gamma=self.gamma,
        )
        estimates = estimator.estimate(self.batch)
        self.mean_ret[name] = estimates["v_target"]
        self.std_ret[name] = estimates["v_target_std"]

    def test_dm_fqe(self):
        name = "dm_fqe"
        estimator = DirectMethod(
            policy=self.algo.get_policy(),
            gamma=self.gamma,
            q_model_config={"type": FQETorchModel, **self.q_model_config},
        )
        self.losses[name] = estimator.train(self.batch)
        estimates = estimator.estimate(self.batch)
        self.mean_ret[name] = estimates["v_target"]
        self.std_ret[name] = estimates["v_target_std"]

    def test_dr_fqe(self):
        name = "dr_fqe"
        estimator = DoublyRobust(
            policy=self.algo.get_policy(),
            gamma=self.gamma,
            q_model_config={"type": FQETorchModel, **self.q_model_config},
        )
        self.losses[name] = estimator.train(self.batch)
        estimates = estimator.estimate(self.batch)
        self.mean_ret[name] = estimates["v_target"]
        self.std_ret[name] = estimates["v_target_std"]

    def test_ope_in_algo(self):
        results = self.algo.evaluate()
        print("OPE in Algorithm results")
        estimates = results["evaluation"]["off_policy_estimator"]
        mean_est = {k: v["v_target"] for k, v in estimates.items()}
        std_est = {k: v["v_target_std"] for k, v in estimates.items()}

        print("Mean:")
        print(*list(mean_est.items()), sep="\n")
        print("Stddev:")
        print(*list(std_est.items()), sep="\n")
        print("\n\n\n")

    def test_multiple_inputs(self):
        # TODO (Rohan138): Test with multiple input files
        pass


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))
