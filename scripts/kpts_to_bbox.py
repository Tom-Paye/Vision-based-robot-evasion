#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseArray
from geometry_msgs.msg import Pose
from geometry_msgs.msg import Point
from std_msgs.msg import Header

import my_cpp_py_pkg.kalman as kalman
import my_cpp_py_pkg.visuals as visuals

import numpy as np
import math
import time
import copy
import logging


    
def link_dists(pos_body, pos_robot):
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
    
    logger = logging.getLogger(__name__)

    #####################
    # # FOR SIMULATION AND TESTING

    # links_r = np.array([[0, 0, 0], [1, 1, 0]])
    # links_b = np.array([[0, 2, 0], [1, 2, 0]])

    # links_r = np.array([[0, -3, 0], [3, 0, 0]])
    # links_b = np.array([[0, 0, 0], [3, -3, 0]])

    # links_r = np.array([[0, 0, 0], [1, 1, 0]])
    # links_b = np.array([[0, 1, 0], [1, 2, 0]])

    # links_r = np.array([[0, 0, 0], [2, 0, 0]])
    # links_b = np.array([[1, 2, 0], [1, 1, 0]])

    # links_r = np.array([[0, 0, 0], [1, 1, 0], [0, -3, 0], [3, 0, 0],
    #                     [0, 0, 0], [1, 1, 0], [0, 0, 0], [2, 0, 0],
    #                     [0, 0, 0], [1, 1, 0], [0, -3, 0], [3, 0, 0],
    #                     [0, 0, 0], [1, 1, 0], [0, 0, 0], [2, 0, 0]])
    # links_b = np.array([[0, 2, 0], [1, 2, 0], [0, 0, 0], [3, -3, 0],
    #                     [0, 1, 0], [1, 2, 0], [1, 2, 0], [1, 1, 0],
    #                     [0, 2, 0], [1, 2, 0], [0, 0, 0], [3, -3, 0],
    #                     [0, 1, 0], [1, 2, 0], [1, 2, 0], [1, 1, 0]])
    
    # links_r = np.array([[0, 0, 0], [1, 1, 0], [1, 2, 0], [2, 1, 0],
    #                     [3, 1, 0]])
    # links_b = np.array([[1/2, 4, 0], [1, 3, 0], [2, 2, 0], [5/2, 3, 0],
    #                     [4, 3, 0]])
    # pos_b = copy.copy(links_b)
    # pos_r = copy.copy(links_r)
    
    #####################
    
    
    links_b, links_r = pos_body, pos_robot
    
    m = len(links_b)
    n = len(links_r)
    p, q = 0, 0

    #####
    
    if m > n:
        arr_to_roll = links_b
        static_array = links_r
    else:
        arr_to_roll = links_r
        static_array = links_b
    n_rolls = max(m, n)
    n_remove = min(m, n)
    mat_roll = np.array([static_array]*n_rolls)
    mat_static = np.array([static_array]*n_rolls)
    bad_segments = []

    for i in range(n_rolls):
        new_layer = np.roll(arr_to_roll, -i, axis=0)
        new_layer = new_layer[0:min(m, n),:]
        mat_roll[i,:] = new_layer
        bad_segments.append([i, n_rolls-i-1])
    bad_segments = np.array(bad_segments[n_rolls-n_remove+1:])

    if m > n:
        links_b = mat_roll
        links_r = mat_static
    else:
        links_r = mat_roll
        links_b = mat_static

    pseudo_links = np.array([links_r[:, :-1, :], links_b[:, :-1, :]])

    d_r = np.diff(links_r, axis=1)
    d_b = np.diff(links_b, axis=1)
    d_rb = np.diff(pseudo_links, axis=0)[0]

    # calc length of both segments and check if parallel
    len_r = np.linalg.norm(d_r, axis=-1)**2
    len_b = np.linalg.norm(d_b, axis=-1)**2
    if 0 in len_r or 0 in len_b:
        logger().info('err: Link without length')
    R = np.einsum('ijk, ijk->ij', d_r, d_b)
    denom = len_r*len_b - R**2
    S1 = np.einsum('ijk, ijk->ij', d_r, d_rb)
    S2 = np.einsum('ijk, ijk->ij', d_b, d_rb)

    paral = (denom<0.001)
    t = (1-paral) * (S1*len_b-S2*R) / denom
    t = np.nan_to_num(t)
    t = np.clip(t, 0, 1)

    u = (t*R - S2) / (len_b)
    u = np.nan_to_num(u)
    u = np.clip(u, 0, 1)

    t = (u*R + S1) / len_r
    t = np.clip(t, 0, 1)


    tp = np.transpose(np.array([t]*3), (1, 2, 0))
    up = np.transpose(np.array([u]*3), (1, 2, 0))
    diffs_3d = np.multiply(d_r, tp) - np.multiply(d_b, up) - d_rb
    dist = np.sqrt(np.sum(diffs_3d**2, axis=-1))

    distp = copy.copy(dist)
    dist = np.around(dist, decimals=6)
    distp[dist == 0] = 1000
    direc = np.multiply(diffs_3d, 1 / distp[:, :,  np.newaxis])
  
    
    [intersec_b_link, intersec_r_link] = np.nonzero(distp == 1000)
    direc[intersec_b_link, intersec_r_link, :] = [0, 0, 0]  # marks the places where the body and robot are believed to clip into another
    dist[bad_segments[:, 0], bad_segments[:, 1]] = 10 # marks the distances comparing imaginary axes (those that link both ends of each limb directly, for example)
    
    t = np.around(t, decimals=6)
    u = np.around(u, decimals=6)

    [i, j] = np.where(dist == np.min(dist))
    t = t[i, j]
    u = u[i, j]
    dist = dist[i, j]
    direc = direc[i, j,:]
    if m > n:
        closest_r = j
        closest_b = (j+i-1)%m
    else:
        closest_b = j
        closest_r = (j+i)%n

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

    return dist, direc, t, u, closest_r, closest_b


class Subscriber(Node):

    def __init__(self):
        super().__init__('minimal_subscriber')
        
        logger = logging.getLogger(__name__)
        logging.basicConfig(level=logging.DEBUG)

        self.initialize_variables()

        self.subscription_data = self.create_subscription(
            PoseArray,
            'kpt_data',
            self.data_callback,
            10)
        self.subscription_data  # prevent unused variable warning

        self.timer = self.create_timer(0.01, self.dist_callback)
        # self.timer = self.create_timer(0.01, self.kalman_callback)
        
    
    def initialize_variables(self):
        self.reset = 0.0
        self.x = []
        self.bodies = {}
        self.subject = '1'
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
        self.max_dist = 0.2      # distance at which the robot feels a force exerted by proximity to a human
        self.in_dist = 0.005     # distance at which the robot is most strongly pushed bach by a human

    def kalman_callback(self):

        if np.any(self.bodies) and self.subject in self.bodies:
            if not np.any(self.x):
                self.x = [self.bodies[self.subject][0][0][0]]
            else:
                self.x.append(self.bodies[self.subject][0][0][0])
        if len(self.x)>1000:
            kalman.kaltest(self.x)
        

    def dist_callback(self):
        if self.subject in self.bodies:
            self.get_logger().debug('self.reset, np.any(self.bodies[self.subject][self.subject]):')
            self.get_logger().debug(str(self.reset and np.any(self.bodies[self.subject][0])))
            # Make the body outline fit the head
            if self.reset and np.any(self.bodies[self.subject][0]):
                # make one line from the left hand to the right
                arms = np.concatenate([np.flip(self.bodies[self.subject][0], axis=0), self.bodies[self.subject][1]])
                arms_dist, arms_direc, arms_t, arms_u, c_r_a, c_a_r = link_dists(arms, self.placeholder_Pe)
                trunk_dist, trunk_direc, trunk_t, trunk_u, c_r_t, c_t_r = link_dists(self.bodies[self.subject][0], self.placeholder_Pe)

                ###############
                # Plotting the distances
                class geom(): pass
                geom.arm_pos = arms
                geom.trunk_pos = self.bodies[self.subject][2]
                geom.robot_pos = self.placeholder_Pe
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

                min_dist_arms = np.min(arms_dist)
                min_dist_trunk = np.min(trunk_dist)
                min_dist = min(min_dist_arms, min_dist_trunk)
                # self.get_logger().info('Minimum distance:')
                # self.get_logger().info(str(min_dist))
                arms_geom = {'dist':arms_dist, 'direc':arms_direc,
                             't':arms_t      , 'u':arms_u,
                             'closest_r':c_r_a        , 'closest_b':c_a_r}
                trunk_geom = {'dist':trunk_dist, 'direc':trunk_direc,
                             't':trunk_t      , 'u':trunk_u,
                             'closest_r':c_r_t        , 'closest_b':c_t_r}
                return arms_geom, trunk_geom
                
                
    def force_estimator(self, body_geom, robot_pose):
        
        """
        Calculates the force imparted on the robot by the bounding box of another body
        This can be conceptualized as pushing on a part of the robot
        (as opposed to forces on the joint motors, which are determined in another function)
        
        INPUT---------------------------
        body_geom: dict whose elements are the output of the "link_dists"
        
        robot_pose: [M, 3] vector corresponding to robot joint positions (locations in cartesian space)
        
        OUTPUT---------------------------
        force: [N] vector of magnitudes of forces to be applied to the robot
            force is given as a float between 0 and 1, 1 being the strongest
        
        direc: [N, 3] array containing the direction along which the force is applied (with norm of 1)
        
        application_segments: [N] vector of robot segments on which to apply each force (segment 0 has an
                                                                                         extremity at the base)
        application_dist: [N] vector of the distance along a segment at which a force is applied
        
        force_vec: [N, 3] array containing the direction along which the force is applied scaled by the force (0 to 1)
        
        """
        force = copy.copy(body_geom['dist'])
        direc = body_geom['direc']
        application_segments = body_geom['closest_r']
        application_dist = body_geom['t']
        
        force = 1 - (force-self.min_dist)/(self.max_dist - self.min_dist)
        force = np.clip(force, 0, 1)
        
        force_vec = np.repmat(force, [1, 3])*direc
        
    
    def force_translator(self, robot_pose, force_vec, application_segments, application_dist):
        
        """
        Translates body forces on the robot to moments on its joints
        --> assumes infinitely rigid links and 1 DOF joints
        --> for each joint, assumes all other joints are rigid
        
        INPUT---------------------------
        
        robot_pose: [M, 3] vector corresponding to robot joint positions (locations in cartesian space)
            NOTE: joints must be given in physically consecutive order (ie consecutive joints are physically connected
                                                                        by a single segment of the robot)
        
        force_vec: [N, 3] array containing the direction along which the force is applied scaled by the force (0 to 1)
            --> 1 line for each separate BODY force, NOT joint torque
        
        application_segments: [N] vector of robot segments on which to apply each force (segment 0 has an
                                                                                         extremity at the base)
        application_dist: [N] vector of the distance along a segment at which a force is applied
        
        OUTPUT---------------------------
        
        torques: [M] vector of torques on the robot's joints
            --> 1 row per joint
            TODO: define robot position by joint positions to disambiguate torque direction
            TODO: Rescale torques to account for the compliance of other joints and redistribute to prevent stall
            TODO: add a function to calculate robot cartesian positions from angles
            
        
        """
        torques = np.zeros(len(robot_pose))
        
        for i, force in enumerate(force_vec):
            for j, joint in enumerate(robot_pose):
                
                # Determine the axis of the joint
                joint_axis = [1, 1, 1]
                
                # Determine moment of force on the joint
                force_pos = (robot_pose[application_segments[i],:]*(1-application_dist[i]))+\
                            (robot_pose[application_segments[i+1],:]*application_dist[i])
                force_relative_pos = force_pose - joint
                moment_raw = np.cross(force, force_relative_pos)
                
                # scale by the colinearity between moment and joint axis
                moment_scaled = np.dot(moment_raw, joint_axis)
                
                torques[j] += moment_scaled
        
        


    # def label_callback(self, msg):
    #     self.get_logger().info(msg.data)
    #     self.label = msg.data
    #     self.data = []
        
    def data_callback(self, msg):
        # self.get_logger().info(str(msg))
        # region = math.trunc(msg.w)
        limb_dict = {'left':0, 'right':1, 'trunk':2, '_stop':-1}
        region = msg.header.frame_id[2:]
        self.reset = ('stop' in msg.header.frame_id)


        if self.reset:
            if self.subject in self.bodies:
                for id in self.bodies.keys():
                    body = self.bodies[id]
                    # self.get_logger().info('Body' + id + ' Left side')
                    # self.get_logger().info(str(body[0]))
            else:
                pass
        else:
            body_id =msg.header.frame_id[0]
            if body_id == '-':
                body_id = int(msg.header.frame_id[0:2])
            else:
                body_id = int(msg.header.frame_id[0])

            poses_ros = msg.poses
            poses = []
            for i, pose in enumerate(poses_ros):
                poses.append([pose.position.x, pose.position.y, pose.position.z])
            poses = np.array(poses)

            threshold = 0.002
            if str(body_id) in self.bodies:
                # if region == 'left':
                #     if np.shape(self.bodies[str(body_id)][0]) == np.shape(poses):
                #         if np.any(np.linalg.norm(self.bodies[str(body_id)][0]-poses, axis=0) < threshold):
                #             poses = self.bodies[str(body_id)][0]    # do not update position if it has not moved sufficiently?
                #     self.bodies[str(body_id)][0] = poses
                # if region == 'right':
                #     if np.shape(self.bodies[str(body_id)][1]) == np.shape(poses):
                #         if np.any(np.linalg.norm(self.bodies[str(body_id)][1]-poses, axis=0) < threshold):
                #             poses = self.bodies[str(body_id)][1]
                #     self.bodies[str(body_id)][1] = poses
                # if region == 'trunk':
                #     if np.shape(self.bodies[str(body_id)][2]) == np.shape(poses):
                #         if np.any(np.linalg.norm(self.bodies[str(body_id)][2]-poses, axis=0) < threshold):
                #             poses = self.bodies[str(body_id)][2]
                #     self.bodies[str(body_id)][2] = poses
                self.bodies[str(body_id)][limb_dict[region]] = poses
            else:
                    self.bodies[str(body_id)] = [poses[0:3,:], poses[0:3,:], poses]

        

            # data = []
            # data_ros = msg.poses
            # for pose in data_ros:
            #     data.append([pose.position.x, pose.position.y, pose.position.z])
            # data = np.array(data)
            # if region == 'left':
            #     self.data_left = data
            # if region == 'right':
            #     self.data_right = data
            # if region == 'trunk':
            #     self.data_trunk = data


            # if region == 0:
            #     if len(self.data_left) <1: #not np.any(self.data_left):
            #         self.data_left = np.array([[msg.x, msg.y, msg.z]])
            #     else:
            #         self.data_left = np.append(self.data_left, np.array([msg.x, msg.y, msg.z]))
            # if region == 1:
            #     if len(self.data_right) <1: #not np.any(self.data_right):
            #         self.data_right = np.array([[msg.x, msg.y, msg.z]])
            #     else:
            #         self.data_right = np.append(self.data_right, np.array([msg.x, msg.y, msg.z]))
            # if region == 2:
            #     if len(self.data_trunk) <1: #not np.any(self.data_trunk):
            #         self.data_trunk = np.array([[msg.x, msg.y, msg.z]])
            #     else:
            #         self.data_trunk = np.append(self.data_trunk, np.array([msg.x, msg.y, msg.z]))





        


    



def main(args = None):
    
    rclpy.init(args=args)

    subscriber = Subscriber()

    rclpy.spin(subscriber)




    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    subscriber.destroy_node()
    rclpy.shutdown()
    
    print('done')
    
if __name__ == "__main__":
    main()