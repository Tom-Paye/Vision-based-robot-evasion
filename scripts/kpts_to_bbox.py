#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseArray
from geometry_msgs.msg import Pose
from geometry_msgs.msg import Point
from geometry_msgs.msg import TransformStamped
from geometry_msgs.msg import Quaternion
from tf2_msgs.msg import TFMessage
from std_msgs.msg import String
from sensor_msgs.msg import JointState
from urdf_parser_py.urdf import URDF
from urdf_parser_py.urdf import Robot
# import messages_fr3
from messages_fr3.msg import Array2d

import vision_based_robot_evasion.kalman as kalman
import vision_based_robot_evasion.visuals as visuals

import numpy as np
from scipy.spatial.transform import Rotation as R
import math
import time
import copy
import logging
from pathlib import Path
from skimage.util.shape import view_as_windows

import subprocess
import tempfile
from urdfpy import URDF
import trimesh

# import tf2_ros
# import tf_transformations



"""
Right now, this function just reads values for robot and body positions whenever it cans,
and updates accordingly.

This makes speed estimation kinda crap, especially for the human.
Reading the timestamp of received info should help

TODO: Restructure the body geom class so it contains all info for both the arms and torso
TODO: Calculate body speed for the robot and human
    --> Robot : We read the jacobians, so it should be doable by reading joint velocities and multiplying
    --> Human : Naive approach: Divide motion between reads by timestamp diff btw/ reads
                    Problem: High noise
                Naive solution : smoothed model from past values with high weight, new
                measurement with low weight, weighted average of both?
TODO: Create clever law combining effects of distance and relative speed to apply force
    --> multiplicative force calculation : F = A x B
        A is a term depending on distance: Fmax * [!- max(D - Dmin, 0)]
        B is a term depending on speed : exp(V) => smaller force if we are moving away
"""

def force_motion_planner(forces, positions, axes):
    """
    Experiment 1 : 
    This function takes reads the forces applied onto a robot, then generates torques applied closer to the base
    of the robot, with the intention of speeding up the response to a force by making the robot quickly adopt a
    position in which it has a 'direct' degree of freedom along the direction a joint is being pushed

    We can weight the moment on each ancestor joint by how far away the target joint is from their axis

    Experiment 2 : 
    Distribute a force on a joint so it affects all parent joints, to try to force the controller
    to move the base joints
    """
    #####################Experiment 1#########################
    # 1st perform this on a single joint
    # Force is [3]
    # joint is the index of the joint on which the force is applied
    # positions are all the joint positions of the robot
    # axes are the joint axes, [3]

    forces_trans = forces[:,0:3]
    for i, Force in enumerate(forces_trans[1:]):
        if np.linalg.norm(Force)>0:
            joint = i+1
    # joint = 4
    # Force = np.array([0, 1, 0])
    # Force = forces_trans[joint]
    # array with the position of the joint as seen from each ancestor
            dp = np.tile(positions[joint],[joint,1]) - positions[0:joint]
            # array of orthogonal distances from each joint axis
            orthogonal_dist = np.sum(dp * axes[0:joint],axis=1)
            dist_scaling = orthogonal_dist / np.max(np.abs(orthogonal_dist))

            # moments trying to make ach joint axis orthogonal to the force
            f = Force / np.linalg.norm(Force)   # unit vector along force direction
            cross = -np.cross(np.tile(f,(joint,1)), axes[0:joint])   # vectors by which to cross the axes

            # how much to scale the moments relative to the force originally imparted on the axis
            # the more colinear the force and the axes of the ancestor joints, the stronger the moments should be 
            moment_scaling = 20. * ( 1 - \
                                np.min(np.linalg.norm(np.cross( np.tile(f,(joint,1)) , axes[:joint] ),axis=1)) )

            moments = cross * np.tile(dist_scaling,(3,1)).T * moment_scaling
            forces[0:joint,3:] += moments

    # # return moments


    #####################Experiment 2#########################
    
    inverse_torque_scaling = np.array([12, 12, 12, 87, 87, 87, 87])/87
    for i, force in enumerate(forces[1:]):
        force_add = copy.copy(force[0:3])
        forces[i+1, 0:3] = force[0:3] / 2
        forces[0:i+2, 0:3] += np.tile(force_add,(i+2,1)) * np.tile(inverse_torque_scaling[0:i+2],(3,1)).T / 2   # /(i+2)
        
    return forces


                     






class robot_description(Node):

    def __init__(self):
        super().__init__('robot_description')
        self.caught = 0
        self.robot_des = self.create_subscription(
            String,
            'robot_description',
            self.description_callback,
            10)
        # while(rclpy.wait_for_message(1)):
        #     time.sleep(0.01)
        # rclpy.rclpy.rclpy.wait_for_message(1)
        # while not rlcpy.test


    def description_callback(self, des_message):
        # only published to once, at the launch of the controller

        a = des_message
        robot = Robot.from_xml_string(a.data)
        # Check if robot model is valid
        robot.check_valid()
        # Print the robot model
        # print(robot)
        self.robot = robot
        self.caught = 1
        # urdf file read : /home/tom/franka_ros2_ws/install/franka_description/share/franka_description/robots/panda_arm.urdf.xacro

        # Call xacro to convert xacro to urdf
    # subprocess.run(['ros2', 'run', 'xacro', 'xacro', xacro_path, '-o', urdf_path], check=True)
    
    # def the_rest():    
    #     link_poses = self.compute_robot_pose(robot, node)

    #     # Now you can use the link_poses dictionary to visualize or analyze the robot's current pose.
    #     for link_name, pose in link_poses.items():
    #         print(f"Link: {link_name}")
    #         print(pose)
    #     a=2


    
def calculate_transforms(robot, tf_world):
    link_poses = {}
    legacy_link = np.eye(4)
    for link in robot.links:
        link_poses[link.name] = legacy_link
        if link.name in tf_world:
            link_poses[link.name] = create_transform_matrix(tf_world[link.name][0], tf_world[link.name][1])
        legacy_link = link_poses[link.name]
    return link_poses 


def create_transform_matrix(translation, rotation):    
    trans_matrix = np.eye(4)
    trans_matrix[:3, 3] = translation

    rot = R.from_quat(rotation)
    trans_matrix[:3, :3] = rot.as_matrix()

    return trans_matrix




    
def link_dists(pos_body, pos_robot, max_dist):
    """
    Given the positions of two bodies, this function returns:
        - the minimum distance between them
        - the points on each body which are closest to the other
        - the direction connecting both bodies
        
    The bodies are considered as chains, not webs (each keypoint is connected to two other joints MAX)
    
    
    
    INPUT---------------------------
    Takes pos_body: [M, 3] array of keypoint positions IN PHYSICALLY CONSECUTIVE ORDER
    (i.e. don't feed it a hand keypoint followed by the head keypoint or it will assume you have antennae)

    Takes pos_robot: [N, 3] vector corresponding to robot joint positions (locations in cartesian space)

    OUTPUT--------------------------
    Returns dist: [P, Q] array of distances
        P = max(M, N) --> one row for each convolution of the longer effector positions on the shorter effector positions.
            Longer or shorter refers to the number of joints, not physical length
        Q = max(M, N)-1 --> because we only consider the distances from the P-1 segments of the shortest effector
        NOTE: in every row, there is one distance which corresponds to a segment connecting the longest effector end-to-end directly
            This distance should not be taken into account for control, and is set to 10m
        NOTE: dist[i, j] is the distance between the jth link of the smaller effector and the j+ith link of the longer one (mod P)

    Returns direc: [P, Q, 3] array of unit direction vectors along the shortest path between each body_segment-robot_segment pair
        if a segment on the robot is found to be intersecting a segment on the body (dist[i, j] == 0):
            the corresponding vector  direc[i, j, :] is [0, 0, 0]
        NOTE: The direct vectors point from the body to the robot

    Returnt t : [P, Q] array of position of 'intersection point' on each segment of the robot
        0 <= t[i, j] <= 1, as it represents a fraction of the segment length, starting from whichever point has a lower index in pos_robot
        Intersection point : on a segment A, the closest point to the segment B we're measuring a distance to

    Returnt u : [P, Q] array of position of 'intersection point' on each segment of the body
        0 <= u[i, j] <= 1, as it represents a fraction of the segment length, starting from whichever point has a lower index in pos_body

    NOTE: Temporarily modified so discm direc, t and u are only output for the minimum distances, not the whole arrays

    return closest_r : [k] array of indices corresponding to the segments of the robot where the closest points are located
    return closest_b : [k] array of indices corresponding to the segments of the body where the closest points are located

        
    https://www.sciencedirect.com/science/article/pii/0020019085900328 
    
    """


    # TODO: // implement marking of imaginary joints
    # TODO: // articulate body geometry into a format suitable for this function
    # TODO: Make the Kalman Filter 3D
    # TODO: Create estimation of body speeds
    # TODO: make kalman filter real-time
    # TODO: // fix the coordinate system
    
    logger = logging.getLogger('link_dists')

    #####################
    # # FOR SIMULATION AND TESTING

    # joints_r = np.array([[0, 0, 0], [1, 1, 0]])
    # joints_b = np.array([[0, 2, 0], [1, 2, 0]])

    # joints_r = np.array([[0, -3, 0], [3, 0, 0]])
    # joints_b = np.array([[0, 0, 0], [3, -3, 0]])

    # joints_r = np.array([[0, 0, 0], [1, 1, 0]])
    # joints_b = np.array([[0, 1, 0], [1, 2, 0]])

    # joints_r = np.array([[0, 0, 0], [2, 0, 0]])
    # joints_b = np.array([[1, 2, 0], [1, 1, 0]])

    # joints_r = np.array([[0, 0, 0], [1, 1, 0], [0, -3, 0], [3, 0, 0],
    #                     [0, 0, 0], [1, 1, 0], [0, 0, 0], [2, 0, 0],
    #                     [0, 0, 0], [1, 1, 0], [0, -3, 0], [3, 0, 0],
    #                     [0, 0, 0], [1, 1, 0], [0, 0, 0], [2, 0, 0]])
    # joints_b = np.array([[0, 2, 0], [1, 2, 0], [0, 0, 0], [3, -3, 0],
    #                     [0, 1, 0], [1, 2, 0], [1, 2, 0], [1, 1, 0],
    #                     [0, 2, 0], [1, 2, 0], [0, 0, 0], [3, -3, 0],
    #                     [0, 1, 0], [1, 2, 0], [1, 2, 0], [1, 1, 0]])
    
    # joints_r = np.array([[0, 0, 0], [1, 1, 0], [1, 2, 0], [2, 1, 0],
    #                     [3, 1, 0]])
    # joints_b = np.array([[1/2, 4, 0], [1, 3, 0], [2, 2, 0], [5/2, 3, 0],
    #                     [4, 3, 0]])
    # pos_b = copy.copy(joints_b)
    # pos_r = copy.copy(joints_r)
    
    #####################
    
    # TODO:create dummy joint situated in the hand between both fingers, to prevent
    # a link being made between the fingers
    # /TODO: run distance finder on all gazilion links, but during the force transfer, only consider the important 7

    joints_b, joints_r = pos_body, pos_robot # [n_jts x 3 coordinates]
    
    n_joints_b = len(joints_b)
    n_joints_r = len(joints_r)
    p, q = 0, 0

    #####
    
    # Only roll over the larger array
    if n_joints_b > n_joints_r:
        arr_to_roll = joints_b
        array_not_rolled = joints_r
    else:
        arr_to_roll = joints_r
        array_not_rolled = joints_b

    # For every new rotation of the rolled array, one of the links actually doesn't exists, and should be disregarded
    n_rolls = max(n_joints_b, n_joints_r)
    n_compared_rows = min(n_joints_b, n_joints_r)
    # mat_roll = np.array([array_not_rolled]*n_rolls) 
    mat_static = np.array([array_not_rolled]*n_rolls)
    bad_segments = []


    ######## Speed up with fancy indexing: https://stackoverflow.com/questions/57272516/numpy-multiple-numpy-roll-of-1d-input-array
    window_shape = (n_rolls,3)
    mat_to_roll = np.concatenate((arr_to_roll,arr_to_roll[:-1]))
    mat_roll = view_as_windows(mat_to_roll, window_shape)[:,0,0:n_compared_rows,:]
    
    bad_segments = np.vstack((np.arange(n_rolls-n_compared_rows+1,n_rolls), np.arange(n_compared_rows-2,-1, -1))).T
    ############################
    # for i in range(n_rolls):
    #     new_layer = np.roll(arr_to_roll, -i, axis=0)
    #     new_layer = new_layer[0:n_compared_rows,:]
    #     mat_roll[i,:] = new_layer
    #     bad_segments.append([i, n_rolls-1 - i])
    # bad_segments = np.array(bad_segments[n_rolls-n_compared_rows+1:]) # +1 because there is 1 less links than segments

    if n_joints_b > n_joints_r:
        joints_b = mat_roll         # [n_jts x n_jnts_2 x 3 coordinates]
        joints_r = mat_static
    else:
        joints_r = mat_roll
        joints_b = mat_static

    link_origins = np.array([joints_r[:, :-1, :], joints_b[:, :-1, :]])

    links_r = np.diff(joints_r, axis=1)     # [n_jts x n_jnts_2-1 x 3 coordinates]
    links_b = np.diff(joints_b, axis=1)
    links_r_b = np.diff(link_origins, axis=0)[0]    # [n_jts x n_jnts_2-1 x 3 coordinates], the [0] is to remove 4th dim

    # Step 1
    # calc length of both segments and check if parallel
    D1 = np.linalg.norm(links_r, axis=-1)**2         # [n_jts x n_jnts_2-1]
    D2 = np.linalg.norm(links_b, axis=-1)**2

    # for all zero-length links, pretend length is very small, so the distance is 0. We need to set their m to 0 later on
    # This exists solely so numpy will stop throwing runtime errors about division by zero
    zeros_r = D1 == 0
    D1[zeros_r] = 0.0001
    zeros_b = D2 == 0
    D2[zeros_b] = 0.0001

    # if 0 in D1 or 0 in D2:
        # logger.info('err: Link without length')

    R = np.einsum('ijk, ijk->ij', links_r, links_b)     # [n_jts x n_jnts_2-1]
    S1 = np.einsum('ijk, ijk->ij', links_r, links_r_b)
    S2 = np.einsum('ijk, ijk->ij', links_b, links_r_b)

    denom = D1*D2 - R**2                                # [n_jts x n_jnts_2-1]
    paral = (np.abs(denom)<0.001)
    denom[paral] = 0.0001


    # Step 2
    t = (1-paral) * (S1*D2-S2*R) / denom            # t corresponds to the robot. it is the fraction of length along each link
    # t = np.nan_to_num(t)                              [n_jts x n_jnts_2-1]
    t = np.clip(t, 0, 1)
    t[zeros_r] = 0

    # Step 3
    u = (t*R - S2) / (D2)                           # u corresponds to the robot. it is the fraction of length along each link
    # u = np.nan_to_num(u)
    u = np.clip(u, 0, 1)
    u[zeros_b] = 0

    # Step 4
    t = (u*R + S1) / D1
    # t = np.nan_to_num(t)
    t = np.clip(t, 0, 1)
    t[zeros_r] = 0

    # Step 5
    link_scaling_r = np.transpose(np.array([t]*3), (1, 2, 0))       # [n_jts x n_jnts_2-1 x 3 dims]
    link_scaling_b = np.transpose(np.array([u]*3), (1, 2, 0))
    diffs_3d = links_r * link_scaling_r - links_b * link_scaling_b - links_r_b  # 3d vectors btw each link pair
    dist = np.sqrt(np.sum(diffs_3d**2, axis=-1))    # DD    [n_jts x n_jnts_2-1]

    # Unit direction betwen each link pair. Where both links intersect, set the direction close to 0 instead
    distp = copy.copy(dist)
    dist = np.around(dist, decimals=6)
    distp[dist == 0] = 1000
    direc = np.multiply(diffs_3d, 1 / distp[:, :,  np.newaxis])     # [n_jts x n_jnts_2-1 x 3 dims]
  
    
    [intersec_b_link, intersec_r_link] = np.nonzero(distp == 1000)
    direc[intersec_b_link, intersec_r_link, :] = [0, 0, 0]  # marks the places where the body and robot are believed to clip into another
    dist[bad_segments[:, 0], bad_segments[:, 1]] = 10 # marks the distances comparing imaginary axes (those that link both ends of each limb directly, for example)
    
    t = np.around(t, decimals=6)
    u = np.around(u, decimals=6)

    # Fetch only the information related to the closest links
    # [i, j] = np.where(dist == np.min(dist))
     # Fetch only the information related to links closer than self.max_dist
    [i, j] = np.where(dist < max_dist)
    # if len(i)>1:
    #     a = 2
    t = t[i, j]                         # [N]
    u = u[i, j]                         # [N]
    dist = dist[i, j]                   # [N]
    direc = direc[i, j,:]               # [N x 3 dims]
    if n_joints_b > n_joints_r:     
        closest_r = j                   # [N]
        closest_b = (j+i)%n_joints_b -1     # I think the -1 is superfluous
    else:
        closest_b = j
        closest_r = (j+i)%n_joints_r -1 # [N]

    # #######################
    # # Plots for testing purposes
    # class geom(): pass
    # geom.arm_pos = pos_b
    # geom.trunk_pos = pos_b
    # geom.robot_pos = pos_r
    # geom.arm_cp_idx = closest_b
    # geom.u = u
    # geom.trunk_cp_idx = closest_b
    # geom.v = u
    # geom.robot_cp_arm_idx = closest_r
    # geom.s = t
    # geom.robot_cp_trunk_idx = closest_r
    # geom.t = t

    # visuals.plot_skeletons(0, geom)
    # #######################

    # convert all results so only the 'base' end of a link is used (i.e. never have t or u == 1)
    closest_r = closest_r + np.floor(t)
    t = t * (1 - np.floor(t))
    
    closest_b = closest_b + np.floor(u)
    u = u * (1 - np.floor(u))

    # remove repeated values so we only have separate pairs
    if len(closest_r) > 1:
        full_info = np.hstack((dist[:, np.newaxis], direc,\
                               t[:, np.newaxis], u[:, np.newaxis], closest_r[:, np.newaxis], closest_b[:, np.newaxis]))
        unq = np.unique(full_info, axis=0)
        dist = unq[:,0]
        direc = unq[:,1:4]
        t = unq[:,4]
        u = unq[:,5]
        closest_r = unq[:,6].astype(int)
        closest_b = unq[:,7].astype(int)

    closest_r = closest_r.astype(int)
    closest_b = closest_b.astype(int)

    # if len(closest_r) >1:
    #     a=2

    return dist, direc, t, u, closest_r, closest_b


class kpts_to_bbox(Node):

    def __init__(self):
        super().__init__('minimal_subscriber')
        
        self.logger = logging.getLogger('kpts_to_bbox')
        logging.basicConfig(level=logging.DEBUG)

        self.initialize_variables()

        # get the robot description
        caught = 0
        description_node = robot_description()
        self.logger.info('Waiting for robot_description message...')
        while not caught:
            rclpy.spin_once(description_node)
            caught = description_node.caught
            time.sleep(0.01)
        self.robot = description_node.robot
        self.logger.info('Robot model loaded!')

        self.subscription_data = self.create_subscription(
            Array2d,      # PoseArray, Array2d
            'kpt_data',
            self.data_callback,
            10)
        self.subscription_data  # prevent unused variable warning

        # self.robot_joint_state = self.create_subscription(
        #     JointState,
        #     'franka/joint_states',
        #     self.get_joint_velocities(),
        #     10)
        
        self.robot_joint_state = self.create_subscription(
            TFMessage,
            'tf',
            self.transform_callback,
            10)
        
        
        # link_poses = self.compute_robot_pose(robot, node)
        
        self.force_publisher_ = self.create_publisher(Array2d, 'repulsion_forces', 10)

        self.timer = self.create_timer(0.005, self.dist_callback)
        # self.timer = self.create_timer(0.01, self.kalman_callback)
        
    
    def initialize_variables(self):
        self.reset = 0.0
        self.x = []
        self.bodies = {}
        self.subject = '0'
        obj = time.gmtime(0) 
        epoch = time.asctime(obj) 
        self.placeholder_Pe = np.array([[0., 0., 0.],
                                        [0., -.1, .3],
                                        [0., .1, .3],
                                        [0., 0., .6],
                                        [.1, .1, .7],
                                        [.1, -.1, .7],
                                        [.2, 0., .8],
                                        [.9, .2, .8],
                                        [.9, 0., .8],
                                        [1., 0., .8],
                                        [1., -.1, .6],
                                        [1., .1, .6],])    # placeholder end effector positions
        self.fig = 0
        self.max_dist = 0.5      # distance at which the robot feels a force exerted by proximity to a human
        self.min_dist = 0.05     # distance at which the robot is most strongly pushed back by a human
        self.joint_pos = [0., -0.8, 0., 2.36, 0., 1.57, 0.79]
        self.joint_vel = [0., -0.0, 0., -0., 0., 0., 0.]
        self.robot_cartesian_positions = np.zeros((7, 3))
        self.offset = np.array([0., 0., 0.]) # position offset to correct zed bullshit

        # publishing stats
        self.pub_counter = 0
        self.Dt = 0
        self.t0 = time.time()

        # debugging
        self.debug_t = time.time()
        self.debug_dt = 0.
        self.debug_dist_loops = 0
        

    def dist_callback(self):
        
        t0 = time.time()
        


        if bool(self.bodies):
            # if you move out of frame too long, you will be assigned a new body ID
            # so we also need to increment it here


            # Scan to find currently active bodies:
            deceased = []
            for subject in self.bodies:
                ct = time.time()
                # expect bodies to be updated at 50 Hz
                if ct - self.bodies[subject][3][0]>0.02 or not np.any(self.bodies[subject][0]):
                    deceased.append(subject)
            for body in deceased: del self.bodies[body] 

        # t1 = time.time()
        # dt = t1 - t0
        # if dt>0.05:
        #     self.logger.info("dist_callback 1 takes " + str(np.round(dt, 4)) + " seconds")   


        if bool(self.bodies):
            # Switch to using oldest known body
            subjects = list(self.bodies.keys())
            subject = np.min(np.array(subjects).astype(int))
            self.subject = str(subject)

        if not bool(self.bodies):
            t0 = time.time()
            dt = t0 - self.debug_t
            # self.debug_dt = self.debug_dt + dt
            self.debug_dist_loops = self.debug_dist_loops + 1
            self.debug_t = t0
            self.debug_dt = 0.

            # if self.debug_dt>0.05:
            #     self.logger.info("dist_callback took " + str(np.round(self.debug_dt, 4)) + " seconds total")
            #     self.logger.info("It ran "+str(self.debug_dist_loops)+" times without getting a good body") 
            #     self.debug_dt = 0.
            #     self.debug_dist_loops = 0

        # t2 = time.time()
        # dt = t2 - t1
        # if dt>0.05:
        #     self.logger.info("dist_callback 2 takes " + str(np.round(dt, 4)) + " seconds") 
        

        if bool(self.bodies):

            self.debug_dt = t0 - self.debug_t
            self.debug_t = t0

            # if not np.any(self.bodies[self.subject][0]) or self.bodies[self.subject][3][0]>5 :
            #     # sometimes the first subject sent is 1, idk why
            #     for i in range(3):
            #         new_subject = str(int(self.subject)+i)
            #         if new_subject in self.bodies:
            #             self.subject = new_subject
            #             break
            
                # make one line from the left hand to the right
            arms = np.concatenate([np.flip(self.bodies[self.subject][0], axis=0), self.bodies[self.subject][1]])
            trunk = self.bodies[self.subject][2]
            robot_pos = self.robot_cartesian_positions

            arms_dist, arms_direc, arms_t, arms_u, c_r_a, c_a_r = link_dists(arms, robot_pos, self.max_dist) # self.placeholder_Pe, robot_pos
            trunk_dist, trunk_direc, trunk_t, trunk_u, c_r_t, c_t_r = link_dists(trunk, robot_pos, self.max_dist)
            # self.placeholder_Pe, robot_pos

            ###############
            # Plotting the distances
            class geom(): pass
            geom.arm_pos = arms
            geom.trunk_pos = trunk
            geom.robot_pos = robot_pos  # self.placeholder_Pe, robot_pos
            geom.arm_cp_idx = c_a_r
            geom.u = arms_u
            geom.trunk_cp_idx = c_t_r
            geom.v = trunk_u
            geom.robot_cp_arm_idx = c_r_a
            geom.s = arms_t
            geom.robot_cp_trunk_idx = c_r_t
            geom.t = trunk_t

            self.fig = visuals.plot_skeletons(self.fig, geom)

            ###############

            # only continue if the safety bubbles have been breached
            if not np.any(arms_dist):
                body_geom = {'dist':trunk_dist, 'direc':trunk_direc,
                            't':trunk_t      , 'u':trunk_u,
                            'closest_r':c_r_t        , 'closest_b':c_t_r}
            elif not np.any(trunk_dist):
                body_geom = {'dist':arms_dist, 'direc':arms_direc,
                            't':arms_t      , 'u':arms_u,
                            'closest_r':c_r_a        , 'closest_b':c_a_r}
            else:
                # min_dist_arms = np.min(arms_dist)
                # min_dist_trunk = np.min(trunk_dist)
                # min_dist = min(min_dist_arms, min_dist_trunk)
                # self.get_logger().info('Minimum distance:')
                # self.get_logger().info(str(min_dist))

                # # if only using the minimum distance
                # if min_dist_arms < min_dist_trunk:
                #     body_geom = {'dist':arms_dist, 'direc':arms_direc,
                #                 't':arms_t      , 'u':arms_u,
                #                 'closest_r':c_r_a        , 'closest_b':c_a_r}
                # else:
                #     body_geom = {'dist':trunk_dist, 'direc':trunk_direc,
                #                 't':trunk_t      , 'u':trunk_u,
                #                 'closest_r':c_r_t        , 'closest_b':c_t_r}
                    
                # if using every distance under max_dist
                dist = np.hstack((arms_dist, trunk_dist))
                direc = np.concatenate((arms_direc, trunk_direc), axis = 0)
                t = np.hstack((arms_t, trunk_t))
                u = np.hstack((arms_u, trunk_u))
                closest_r = np.hstack((c_r_a, c_r_t))
                closest_b = np.hstack((c_a_r, c_t_r+len(c_a_r)))
                body_geom = {'dist':dist, 'direc':direc,
                                't':t      , 'u':u,
                                'closest_r':closest_r        , 'closest_b':closest_b}
                # forces = self.force_estimator(body_geom, robot_pos)    # self.placeholder_Pe, robot_pos

                # t3 = time.time()
                # dt = t3 - t2
                # if dt>0.05:
                #     self.logger.info("dist_callback 3 takes " + str(np.round(dt, 4)) + " seconds") 

                if time.time()>5:

                #     # forces = np.zeros((7, 6))
                #     # forces[3, 2] = 20

                #     self.generate_repulsive_force_message(forces)       
                # 

                    self.generate_distance_message(body_geom, robot_pos) 



    def generate_distance_message(self, body_geom, robot_pose):
        """
        -   Takes in the geometry of the robot as well as link distances
        -   Translates link distances to force precursors
        -   publishes the result out as a 1D vector
        """
        # Input : clip(max_dist-min_dist - abs(x-min_dist))     * application_dist if torque

        t0 = time.time()

        direc = body_geom['direc']                      # unit vectors associated with each force
        application_segments = body_geom['closest_r']   # segment on which the force is applied
        application_dist = body_geom['t']               # fraction of each segment at which each force is applied

        levers = np.diff(robot_pose, axis = 0)          # vectors pointing along the robot links
        link_lengths = np.linalg.norm(levers, axis=1)
        

        dists = copy.copy(body_geom['dist'])

        full_force_vec = np.zeros([len(robot_pose)-1, 6])


        #####################
        spring_dists = np.clip(self.max_dist - self.min_dist - np.abs(dists-self.min_dist), 0, self.max_dist - self.min_dist)
        spring_vecs = spring_dists[:, np.newaxis] * direc

        # add and group spring vectors by joint
        joint_springs = []
        for i in range(len(robot_pose)):
            joint_springs.append( np.sum(spring_vecs[application_segments == i], axis=0) )

        # remove the 1st joint, which is the ground
        full_force_vec[:, 0:3] = np.array(joint_springs)[1:]



        # #####################

        # for i, force in enumerate(dists):
        #     vec = direc[i,:]
        #     seg = application_segments[i]
        #     dist = application_dist[i]

        #     force = self.max_dist - self.min_dist - np.abs(force-self.min_dist) #/(self.max_dist - self.min_dist)
        #     force = np.clip(force,0,self.max_dist - self.min_dist)
            

        #     force_vec = force * vec
        #     moment = np.zeros(3)

        #     if dist > 0:
        #         lever = levers[seg,:]
        #         moment = np.cross(lever*dist, force_vec)
            
        #     full_force_vec[seg,:] = full_force_vec[seg,:] + np.hstack((force_vec, moment))

        ####################ACCOUNT FOR INPUT OF SIZE 13###################
        # For every joint after the last movable joint(link_1-7), apply its torque/forces to the previous joint
        for i in range(len(full_force_vec)-7):
            l = link_lengths[-i-1]
            if l == 0:
                length_multiplier = np.eye(6)
            else:
                # length_multiplier_force = np.concatenate((np.eye(3), -np.eye(3)/l), axis=0)
                length_multiplier_force = np.concatenate((np.eye(3), np.zeros((3, 3))), axis=0)
                # length_multiplier_moment = np.concatenate((np.eye(3)*l, np.eye(3)), axis=0)
                # consider that the links towards the EE are short enough to be the same point,
                # else the fingers will cause moment trouble
                length_multiplier_moment = np.concatenate((np.zeros((3, 3)), np.zeros((3, 3))), axis=0)
                length_multiplier = np.concatenate((length_multiplier_force, length_multiplier_moment), axis=1)
            full_force_vec[-i-2] += length_multiplier @ full_force_vec[-i-1].T
        
        # # Remove joint 0, apply only its torque to link 1
        # l = link_lengths[0]
        # if l == 0:
        #     length_multiplier = np.eye(6)
        # else:
        #     length_multiplier_force = np.concatenate((np.zeros((3,3)), np.zeros((3,3))), axis=0)
        #     length_multiplier_moment = np.concatenate((np.zeros((3,3)), np.eye(3)), axis=0)
        #     length_multiplier = np.concatenate((length_multiplier_force, length_multiplier_moment), axis=1)
        # full_force_vec[1] += length_multiplier @ full_force_vec[0]   

        full_force_vec = full_force_vec[0:7] / 4    # TODO: Transfer this dactor to the controller instead

        full_force_vec = np.nan_to_num(full_force_vec)

        


        # transform to message
        forces = full_force_vec

        # force_scaling = np.array([87., 87., 87., 87., 12., 12., 12.]) / (self.max_dist - self.min_dist)
        # total_force = force_scaling[:, np.newaxis] * full_force_vec
        # self.logger.info("Total force requested: " + str(total_force) + " N / Nm")

        if np.any(forces):

            forces_flattened = forces.flatten(order='C')

            force_message = Array2d()
            force_message.array = list(forces_flattened.astype(float))
            [force_message.height, force_message.width]  = np.shape(forces.T)
            self.force_publisher_.publish(force_message)

            self.publishing_stats()

        # t1 = time.time()
        # dt = t1 - t0
        # if dt>0.05:
        #     self.logger.info("generate_distance_message takes " + str(np.round(dt, 4)) + " seconds") 


    def force_estimator(self, body_geom, robot_pose):
        
        """
        Calculates the force imparted on the robot by the bounding box of another body
        This can be conceptualized as pushing on a part of the robot
        (as opposed to forces on the joint motors, which are determined in another function)

        For easier calculation, transform each force on a part of a link into:
        (a force at the end of that link closest to the base) + (a moment on that end)

        NOTE: This function assumes all joints are capable of the same torque.
        Another function should be implemented to give physical meanings to the output of this one,
        which merely gives forces/moments relative to the strongest force

        NOTE: The output of this function has one less row than the input, because we take 8 points
        (1 for each joint including the base) + 1 point for the EE, but we only output to the joints

        
        INPUT---------------------------
        body_geom: dict whose elements are the output of the "link_dists"
        
        robot_pose: [8, 3] vector corresponding to robot joint positions (locations in cartesian space)
        
        OUTPUT---------------------------
        forces: [7,3] vector of magnitudes of forces to be applied to the robot in 3D space
            force is given as a float between 0 and 1, 1 being the strongest
        
        // direc: [N, 3] array containing the direction along which the force is applied (with norm of 1)
        
        // application_segments: [N = 8] vector of robot segments on which to apply each force (segment 0 has an
                                                                                         extremity at the base)
        // application_dist: [N] vector of the distance along a segment at which a force is applied
        
        // force_vec: [N, 3] array containing the direction along which the force is applied scaled by the force (0 to 1)

        // moment:    [N, 3] array containing the direction along which the moment is applied scaled by the moment (0 to 1)

        TODO: Implement the rescaling, current forces / moments are left unscaled
        
        """
        

        direc = body_geom['direc']                      # unit vectors associated with each force
        application_segments = body_geom['closest_r']   # segment on which the force is applied
        application_dist = body_geom['t']               # fraction of each segment at which each force is applied

        levers = np.diff(robot_pose, axis = 0)          # vectors pointing along the robot links
        link_lengths = np.linalg.norm(levers, axis=1)
        

        dists = copy.copy(body_geom['dist'])

        full_force_vec = np.zeros([len(robot_pose), 6])



        for i, force in enumerate(dists):
            vec = direc[i,:]
            seg = application_segments[i]
            dist = application_dist[i]

            force = 1 - np.abs(force-self.min_dist)/(self.max_dist - self.min_dist)
            # force = np.clip(force, 0, 1)


            # Rescale forces vector to create actual forces, in Newtons
            # To calculate max force, go to https://frankaemika.github.io/docs/control_parameters.html#limits-for-franka-research-3
            # take the lowest max moment of 12 Nm, divide by 1m to have the max force exerted onto a link bound under a safe limit
            force_max = 87 / 1  #Nm
            force_scaling = force_max   # this works because the force is already scaled between 0 and 1
            force = force * force_scaling




            force_vec = force * vec
            moment = np.zeros(3)

            if dist > 0:
                lever = levers[seg,:]
                moment = np.cross(lever*dist, force_vec)
            
            full_force_vec[seg,:] = full_force_vec[seg,:] + np.hstack((force_vec, moment))

            #######################################################
            # for testing custom forces
            # first row corresponds to base frame, so here arrays start at 1
            # full_force_vec = np.zeros(np.shape(full_force_vec))
            # full_force_vec[4, 1] = 30

            #######################################################

        ####################ACCOUNT FOR INPUT OF SIZE 13###################
        # For every joint after the last movable joint(link_1-7), apply its torque/forces to the previous joint
        # The question is, which links to keep? The ones named 1-7, or the seven with non-zero distance do the preceding?
        for i in range(len(full_force_vec)-7):
            l = link_lengths[-i-1]
            if l == 0:
                length_multiplier = np.eye(6)
            else:
                # length_multiplier_force = np.concatenate((np.eye(3), -np.eye(3)/l), axis=0)
                length_multiplier_force = np.concatenate((np.eye(3), np.zeros((3, 3))), axis=0)
                # length_multiplier_moment = np.concatenate((np.eye(3)*l, np.eye(3)), axis=0)
                # consider that the links towards the EE are short enough to be the same point,
                # else the fingers will cause moment trouble
                length_multiplier_moment = np.concatenate((np.zeros((3, 3)), np.zeros((3, 3))), axis=0)
                length_multiplier = np.concatenate((length_multiplier_force, length_multiplier_moment), axis=1)
            full_force_vec[-i-2] += length_multiplier @ full_force_vec[-i-1].T


        
        # Remove joint 0, apply only its torque to link 1
        l = link_lengths[0]
        if l == 0:
            length_multiplier = np.eye(6)
        else:
            length_multiplier_force = np.concatenate((np.zeros((3,3)), np.zeros((3,3))), axis=0)
            length_multiplier_moment = np.concatenate((np.zeros((3,3)), np.eye(3)), axis=0)
            length_multiplier = np.concatenate((length_multiplier_force, length_multiplier_moment), axis=1)
        full_force_vec[1] += length_multiplier @ full_force_vec[0]   

        full_force_vec = full_force_vec[1:8]
        #################################################################

        # account for some joints being merged: 
        # jts 1 and 2 are superimposed
        # all superimposed jts: 1-2, 5-6, 8-hand
        full_force_vec = np.clip(full_force_vec, 0, force_max)  

        ## Testing ##
        # alternate_force_vec = force_motion_planner(full_force_vec, robot_pose, self.axis_rot)
        # full_force_vec = alternate_force_vec

        full_force_vec = np.clip(full_force_vec, 0, force_max) 
        # Try to rescale forces so we move the base joints more but don't make the EE break
        # the sound barrier
        # torque info at https://frankaemika.github.io/docs/control_parameters.html
        joint_torque_scaling = np.array([87, 87, 87, 87, 12, 12, 12])/87
        forces_rescaled = full_force_vec * np.tile(joint_torque_scaling,(6,1)).T * 0.7
        torque_to_force_scaling = np.array([2, 2, 2, 1, 1, 1])
        forces_rescaled = forces_rescaled * np.tile(torque_to_force_scaling,(7,1))
        


        # output = full_force_vec
        # output = alternate_force_vec
        output = forces_rescaled

        return output

        

    def data_callback(self, msg):


        ################
        # limb_dict = {'left':0, 'right':1, 'trunk':2, '_stop':-1}
        # Reshape message into array
        t0 = time.time()
        n_rows = msg.height
        n_cols = msg.width
        msg_array = np.array(msg.array)
        kpt_array = np.reshape(msg_array,(n_rows,n_cols))
        for body in np.unique(kpt_array[:,0]):
            body_kpts = kpt_array[kpt_array[:,0]==body,1:]
            if not str(int(body)) in self.bodies:
                self.bodies[str(int(body))] = [[], [], [], [time.time()]]
            for limb in np.unique(body_kpts[:,0]):
                limb_kpts = body_kpts[body_kpts[:,0]==limb,1:]
                # print(limb_kpts)
                offset_mat = np.tile(self.offset, (len(limb_kpts),1))
                limb_kpts = limb_kpts + offset_mat
                # remove rows with nan
                limb_kpts = limb_kpts[~np.isnan(limb_kpts).any(axis=1), :]
                # print(limb_kpts)
                self.bodies[str(int(body))][int(limb)] = limb_kpts
                self.bodies[str(int(body))][3][0] = time.time()

        # t1 = time.time()
        # dt = t1 - t0
        # if dt>0.05:
        #     self.logger.info("data_callback takes " + str(np.round(dt, 4)) + " seconds")   

        # self.dist_callback()


    def transform_callback(self, tf_message):
        
        t0 = time.time()

        t_raw = np.zeros((10,3))
        r_raw = np.zeros((10,4))
        r_raw[:,-1] = np.ones(10)
        joint_name = [None] * 10

        for transform in tf_message.transforms:
            rot_ros = transform.transform.rotation
            trans_ros = transform.transform.translation
            id = transform.child_frame_id
            if id[-1].isnumeric():
                i = int(id[-1])-1
            else:
                if id == 'panda_leftfinger':
                    i = -3
                else:
                    i = -2

            
            vec_t = np.array([trans_ros.x, trans_ros.y, trans_ros.z])
            vec_r = np.array([rot_ros.x, rot_ros.y, rot_ros.z, rot_ros.w])

            t_raw[i+1] = vec_t
            r_raw[i+1] = vec_r
            joint_name[i+1] = id


        
        robot_translation = copy.copy(t_raw)
        robot_rotation = copy.copy(r_raw)

        total_trans = copy.copy(robot_translation[0])
        total_rot = copy.copy(robot_rotation[0])


        for i in range(len(t_raw)-2):

            t_r = t_raw[i+1]
            r_r = r_raw[i+1]

            #######################
            trm = R.from_quat(total_rot)
            srm = R.from_quat(r_r)
            watch_trm = trm.as_matrix()
            watch_srm = srm.as_matrix()


            ntrm = trm * srm
            watch_ntrm = np.around(ntrm.as_matrix(),3)
            watch_quat = ntrm.as_quat()
            total_rot = watch_quat
            a = 2
            #######################


            quat_rot = R.from_quat(robot_rotation[i]).apply(t_r)
            # inv_rot = [-total_rot[0], total_rot[1], total_rot[2], total_rot[3]]
            total_trans = total_trans + quat_rot

            
            
            robot_rotation[i+1] = total_rot
            robot_translation[i+1] = total_trans

        # repeat the last step manually for the left finger, which is based on the hand and not the right finger
        robot_rotation[-1] = robot_rotation[-2]
        quat_rot = R.from_quat(robot_rotation[-3]).apply(t_raw[-1])
        robot_translation[-1] =  robot_translation[-3] + quat_rot

        # # add a line between the fingers so links will always go through the hand
        # robot_rotation = np.insert(robot_rotation, -1, robot_rotation[-3], axis=0)
        # robot_translation = np.insert(robot_translation, -1, robot_translation[-3], axis=0)

        # self.logger.info('body: \n', str(robot_translation))

        # self.robot_cartesian_positions = robot_translation

        tf_world = {}
        for i, joint in enumerate(joint_name[1:]):
            tf_world[joint] = [robot_translation[i+1], robot_rotation[i+1]]
        # tf_world = dict.fromkeys(joint_name, [robot_translation, robot_rotation])
        
        link_poses = calculate_transforms(self.robot, tf_world)
        pos_world = np.array(list(link_poses.values()))[:, 0:3, -1]

        self.robot_cartesian_positions = pos_world

        # apply robot rotations to each robot axis, assuming that axis is locally [0, 0, 1]
        joint_axes = np.zeros((7, 3))
        joint_axes[:,-1] = np.ones(7)
        self.axis_rot = R.from_quat(robot_rotation[1:-2]).apply(joint_axes)

        # t1 = time.time()
        # dt = t1 - t0
        # if dt>0.05:
        #     self.logger.info("transform_callback takes " + str(np.round(dt, 4)) + " seconds")
        # return link_poses
    
    def publishing_stats(self):

        t = time.time()
        dt = t - self.t0
        self.Dt = self.Dt + dt
        self.t0 = t
        self.pub_counter = self.pub_counter +1

        if dt>0.5:
            self.get_logger().info("Distances published after "+str(np.round(dt, 3))+" seconds")
            self.get_logger().info("It took "+str(t-self.debug_t)+" seconds to run dist_callback")
            self.get_logger().info("Dist had been spinning on empty for "+str(self.debug_dist_loops)+" cycles")
            self.get_logger().info("Dist was first called "+str(self.debug_dt)+" seconds after it last ended")
            
        self.debug_dist_loops = 0

        self.debug_t = time.time()
        self.debug_dt = 0.

        if self.Dt > 10:
            pub_freq = np.round(self.pub_counter / self.Dt, 3)
            self.get_logger().info("Distances published at "+str(pub_freq)+" Hz")
            self.Dt = 0
            self.pub_counter = 0

    

        
   


    



def main(args = None):
    
    rclpy.init(args=args)

    bbox_generator = kpts_to_bbox()

    rclpy.spin(bbox_generator)




    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    bbox_generator.destroy_node()
    rclpy.shutdown()
    
    print('done')
    
if __name__ == "__main__":
    main()
