"""
A script to collect a batch of human demonstrations.

The demonstrations can be played back using the `playback_demonstrations_from_hdf5.py` script.
"""

current_timestamps = 0
import argparse
import datetime
import json
import os
import shutil
import time
from glob import glob

import h5py
import numpy as np
import threading

import pathlib

root_dir = pathlib.Path(__file__).parent.parent.parent
import sys

sys.path.append(str(root_dir))
import robosuite as suite
import robosuite.macros as macros
from robosuite import load_controller_config
from robosuite.utils.input_utils import input2action
from robosuite.wrappers import DataCollectionWrapper, VisualizationWrapper
from scipy.spatial.transform import Rotation


class EnvRunner:
    def __init__(self, env, freq=50):
        self.env = env
        self.freq = freq
        self.action = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def set_action(self, action):
        with self.lock:
            self.action = action

    def run_step(self):
        global current_timestamps
        target_interval = 1 / self.freq
        while not self.stop_event.is_set():
            start_time = time.time()
            with self.lock:
                if self.action is not None:
                    self.env.step(self.action)
                    current_timestamps += 1

            end_time = time.time()
            elapsed = end_time - start_time

            sleep_time = target_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
            # print(f"Frequency: {1 / (time.time() - start_time)}")


def collect_human_trajectory(env, device, arm, env_configuration):
    """
    Use the device (keyboard or SpaceNav 3D mouse) to collect a demonstration.
    The rollout trajectory is saved to files in npz format.
    Modify the DataCollectionWrapper wrapper to add new fields or change data formats.

    Args:
        env (MujocoEnv): environment to control
        device (Device): to receive controls from the device
        arms (str): which arm to control (eg bimanual) 'right' or 'left'
        env_configuration (str): specified environment configuration
    """
    deadzone = np.array([0, 0, 0, 0.1, 0.1, 0.1])
    env.reset()

    # ID = 2 always corresponds to agentview
    env.render()

    env_runner = EnvRunner(env)
    step_thread = threading.Thread(target=env_runner.run_step)
    step_thread.start()
    max_steps = 10 * env_runner.freq
    global current_timestamps

    task_completion_hold_count = -1  # counter to collect 10 timesteps after reaching goal
    device.start_control()

    # Loop until we get a reset from the input or the task completes
    try:
        while True:
            # Set active robot
            active_robot = env.robots[0] if env_configuration == "bimanual" else env.robots[arm == "left"]

            # Get the newest action
            action, grasp = input2action(
                device=device, robot=active_robot, active_arm=arm, env_configuration=env_configuration
            )
            if action is not None:
                tmp_xyz = action[:6]
                is_dead = (-deadzone < tmp_xyz) & (tmp_xyz < deadzone)
                tmp_xyz[is_dead] = 0
                action[:6] = tmp_xyz
            # If action is none, then this a reset so we should break
            if action is None:
                break

            # Run environment step
            # env.step(action)
            env_runner.set_action(action)
            env.render()

            # Also break if we complete the task
            if task_completion_hold_count == 0:
                break

            # state machine to check for having a success for 10 consecutive timesteps
            if env._check_success():
                if task_completion_hold_count > 0:
                    task_completion_hold_count -= 1  # latched state, decrement count
                else:
                    task_completion_hold_count = 10  # reset count on first success timestep
            else:
                task_completion_hold_count = -1  # null the counter if there's no success

            if current_timestamps >= max_steps:
                current_timestamps = 0
                break
    except KeyboardInterrupt as e:
        env_runner.stop_event.set()
        step_thread.join()
        raise e

    # cleanup for end of data collection episodes
    env_runner.stop_event.set()
    step_thread.join()
    env.close()


def gather_demonstrations_as_hdf5(directory, out_dir, env_info):
    """
    Gathers the demonstrations saved in @directory into a
    single hdf5 file.

    The strucure of the hdf5 file is as follows.

    data (group)
        date (attribute) - date of collection
        time (attribute) - time of collection
        repository_version (attribute) - repository version used during collection
        env (attribute) - environment name on which demos were collected

        demo1 (group) - every demonstration has a group
            model_file (attribute) - model xml string for demonstration
            states (dataset) - flattened mujoco states
            actions (dataset) - actions applied during demonstration

        demo2 (group)
        ...

    Args:
        directory (str): Path to the directory containing raw demonstrations.
        out_dir (str): Path to where to store the hdf5 file.
        env_info (str): JSON-encoded string containing environment information,
            including controller and robot info
    """

    hdf5_path = os.path.join(out_dir, "demo.hdf5")
    f = h5py.File(hdf5_path, "w")

    # store some metadata in the attributes of one group
    grp = f.create_group("data")

    num_eps = 0
    env_name = None  # will get populated at some point

    for ep_directory in os.listdir(directory):
        state_paths = os.path.join(directory, ep_directory, "state_*.npz")
        states = []
        actions = []
        centric_obj_pose = []
        subtask_begin_index = []
        success = True

        for state_file in sorted(glob(state_paths)):
            dic = np.load(state_file, allow_pickle=True)
            env_name = str(dic["env"])

            states.extend(dic["states"])
            for ai in dic["action_infos"]:
                actions.append(ai["actions"])
            centric_obj_pose.extend(dic["centric_obj_pose"])
            subtask_begin_index.extend(dic["subtask_begin_index"])
            success = success or dic["successful"]

        if len(states) == 0:
            continue

        # Add only the successful demonstration to dataset
        if success:
            print("Demonstration is successful and has been saved")
            # Delete the last state. This is because when the DataCollector wrapper
            # recorded the states and actions, the states were recorded AFTER playing that action,
            # so we end up with an extra state at the end.
            del states[-1]
            assert len(states) == len(actions)

            ep_data_grp = grp.create_group("demo_{}".format(num_eps))
            num_eps += 1

            # store model xml as an attribute
            xml_path = os.path.join(directory, ep_directory, "model.xml")
            with open(xml_path, "r") as f:
                xml_str = f.read()
            ep_data_grp.attrs["model_file"] = xml_str

            # write datasets for states and actions
            ep_data_grp.create_dataset("states", data=np.array(states))
            ep_data_grp.create_dataset("actions", data=np.array(actions))
            ep_data_grp.create_dataset("centric_obj_pose", data=np.array(centric_obj_pose))
            ep_data_grp.create_dataset("subtask_begin_index", data=np.array(subtask_begin_index))
        else:
            print("Demonstration is unsuccessful and has NOT been saved")

    # write dataset attributes (metadata)
    now = datetime.datetime.now()
    grp.attrs["date"] = "{}-{}-{}".format(now.month, now.day, now.year)
    grp.attrs["time"] = "{}:{}:{}".format(now.hour, now.minute, now.second)
    grp.attrs["repository_version"] = suite.__version__
    grp.attrs["env"] = env_name
    grp.attrs["env_info"] = env_info

    f.close()


if __name__ == "__main__":
    # Arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory",
        type=str,
        default=os.path.join(suite.models.assets_root, "demonstrations"),
    )
    parser.add_argument("--environment", type=str, default="Lift")
    parser.add_argument("--robots", nargs="+", type=str, default="Panda", help="Which robot(s) to use in the env")
    parser.add_argument(
        "--config", type=str, default="single-arm-opposed", help="Specified environment configuration if necessary"
    )
    parser.add_argument("--arm", type=str, default="right", help="Which arm to control (eg bimanual) 'right' or 'left'")
    parser.add_argument("--camera", type=str, default="agentview", help="Which camera to use for collecting demos")
    parser.add_argument(
        "--controller", type=str, default="OSC_POSE", help="Choice of controller. Can be 'IK_POSE' or 'OSC_POSE'"
    )
    parser.add_argument("--device", type=str, default="keyboard")
    parser.add_argument("--pos-sensitivity", type=float, default=1.0, help="How much to scale position user inputs")
    parser.add_argument("--rot-sensitivity", type=float, default=1.0, help="How much to scale rotation user inputs")
    parser.add_argument("--control_freq", type=int, default=30)
    args = parser.parse_args()

    # Get controller config
    controller_config = load_controller_config(default_controller=args.controller)

    # Create argument configuration
    config = {
        "robots": args.robots,
        "controller_configs": controller_config,
        "control_freq": args.control_freq,
    }

    # Check if we're using a multi-armed environment and use env_configuration argument if so
    if "TwoArm" in args.environment:
        config["env_configuration"] = args.config

    # Create environment
    env = suite.make(
        **config,
        env_name=args.environment,
        has_renderer=True,
        has_offscreen_renderer=False,
        render_camera=args.camera,
        ignore_done=True,
        use_camera_obs=False,
        reward_shaping=True,
    )

    # Wrap this with visualization wrapper
    env = VisualizationWrapper(env)

    # Grab reference to controller config and convert it to json-encoded string
    env_info = json.dumps(config)

    # wrap the environment with data collection wrapper
    tmp_directory = "/tmp/{}".format(str(time.time()).replace(".", "_"))
    env = DataCollectionWrapper(env, tmp_directory)

    # initialize device
    if args.device == "keyboard":
        from robosuite.devices import Keyboard

        device = Keyboard(pos_sensitivity=args.pos_sensitivity, rot_sensitivity=args.rot_sensitivity)
    elif args.device == "spacemouse":
        from robosuite.devices import SpaceMouse

        device = SpaceMouse(pos_sensitivity=args.pos_sensitivity, rot_sensitivity=args.rot_sensitivity)
    else:
        raise Exception("Invalid device choice: choose either 'keyboard' or 'spacemouse'.")

    # make a new timestamped directory
    t1, t2 = str(time.time()).split(".")
    new_dir = os.path.join(args.directory, "{}_{}".format(t1, t2))
    os.makedirs(new_dir)

    idx = 0
    # collect demonstrations
    while True:
        current_timestamps = 0
        print(f"----------------Collected {idx} demonstrations----------------")
        collect_human_trajectory(env, device, args.arm, args.config)

        print(env.ep_directory)
        if input("Save this demonstration? ([y]/n): ").lower() in {"n", "no"}:
            env.reset()
            shutil.rmtree(env.ep_directory)
            continue
        gather_demonstrations_as_hdf5(tmp_directory, new_dir, env_info)

        idx += 1
