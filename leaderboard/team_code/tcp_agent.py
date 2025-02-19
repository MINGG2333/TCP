import os
import json
import datetime
import pathlib
import time
import cv2
import carla
from collections import deque
import math
from collections import OrderedDict

import torch
import carla
import numpy as np
from PIL import Image
from torchvision import transforms as T

from leaderboard.autoagents import autonomous_agent

from TCP.model import TCP
from TCP.config import GlobalConfig
from team_code.planner import RoutePlanner


SAVE_PATH = os.environ.get('SAVE_PATH', None)

# jxy: addition; (add display.py and fix RoutePlanner.py)
from team_code.display import HAS_DISPLAY, Saver, debug_display
# addition from team_code/map_agent.py
from carla_project.src.common import CONVERTER, COLOR
from carla_project.src.carla_env import draw_traffic_lights, get_nearby_lights


def get_entry_point():
	return 'TCPAgent'


class TCPAgent(autonomous_agent.AutonomousAgent):
	def setup(self, path_to_conf_file):
		self.track = autonomous_agent.Track.SENSORS
		self.config_path = path_to_conf_file
		self.step = -1
		self.wall_start = time.time()
		self.initialized = False

		return AgentSaver

		# jxy: add return AgentSaver and init_ads (setup keep 5 lines); rm save_path;
	def init_ads(self, path_to_conf_file):

		self.alpha = 0.3
		self.status = 0
		self.steer_step = 0
		self.last_moving_status = 0
		self.last_moving_step = -1
		self.last_steers = deque()

		self.config = GlobalConfig()
		self.net = TCP(self.config)


		ckpt = torch.load(path_to_conf_file)
		ckpt = ckpt["state_dict"]
		new_state_dict = OrderedDict()
		for key, value in ckpt.items():
			new_key = key.replace("model.","")
			new_state_dict[new_key] = value
		self.net.load_state_dict(new_state_dict, strict = False)
		self.net.cuda()
		self.net.eval()

		self.takeover = False
		self.stop_time = 0
		self.takeover_time = 0

		self._im_transform = T.Compose([T.ToTensor(), T.Normalize(mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225])])

		self.last_steers = deque()
		# self.save_path = None
		# if SAVE_PATH is not None:
		# 	now = datetime.datetime.now()
		# 	string = pathlib.Path(os.environ['ROUTES']).stem + '_'
		# 	string += '_'.join(map(lambda x: '%02d' % x, (now.month, now.day, now.hour, now.minute, now.second)))

		# 	print (string)

		# 	self.save_path = pathlib.Path(os.environ['SAVE_PATH']) / string
		# 	self.save_path.mkdir(parents=True, exist_ok=False)

		# 	(self.save_path / 'rgb').mkdir()
		# 	(self.save_path / 'meta').mkdir()
		# 	(self.save_path / 'bev').mkdir()

	def _init(self):
		torch.cuda.empty_cache()
		self._route_planner = RoutePlanner(4.0, 50.0)
		self._route_planner.set_route(self._global_plan, True)

		self.initialized = True

		super()._init() # jxy add

	def _get_position(self, tick_data):
		gps = tick_data['gps']
		gps = (gps - self._route_planner.mean) * self._route_planner.scale

		return gps

	def sensors(self):
				return [
				{
					'type': 'sensor.camera.rgb',
					'x': -1.5, 'y': 0.0, 'z':2.0,
					'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
					'width': 900, 'height': 256, 'fov': 100,
					'id': 'rgb'
					},
				{
					'type': 'sensor.camera.rgb',
					'x': 0.0, 'y': 0.0, 'z': 50.0,
					'roll': 0.0, 'pitch': -90.0, 'yaw': 0.0,
					'width': 512, 'height': 512, 'fov': 5 * 10.0,
					'id': 'bev'
					},	
				{
					'type': 'sensor.other.imu',
					'x': 0.0, 'y': 0.0, 'z': 0.0,
					'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
					'sensor_tick': 0.05,
					'id': 'imu'
					},
				{
					'type': 'sensor.other.gnss',
					'x': 0.0, 'y': 0.0, 'z': 0.0,
					'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
					'sensor_tick': 0.01,
					'id': 'gps'
					},
				{
					'type': 'sensor.speedometer',
					'reading_frequency': 20,
					'id': 'speed'
					},
				# jxy: addition from team_code/map_agent.py
				{
					'type': 'sensor.camera.semantic_segmentation',
					'x': 0.0, 'y': 0.0, 'z': 100.0,
					'roll': 0.0, 'pitch': -90.0, 'yaw': 0.0,
					'width': 512, 'height': 512, 'fov': 5 * 10.0,
					'id': 'map'
					},
				]

	def tick(self, input_data):
		self.step += 1

		rgb = cv2.cvtColor(input_data['rgb'][1][:, :, :3], cv2.COLOR_BGR2RGB)
		bev = cv2.cvtColor(input_data['bev'][1][:, :, :3], cv2.COLOR_BGR2RGB)
		gps = input_data['gps'][1][:2]
		speed = input_data['speed'][1]['speed']
		compass = input_data['imu'][1][-1]

		if (math.isnan(compass) == True): #It can happen that the compass sends nan for a few frames
			compass = 0.0

		result = {
				'rgb': rgb,
				'gps': gps,
				'speed': speed,
				'compass': compass,
				'bev': bev
				}
		
		pos = self._get_position(result)
		result['gps'] = pos
		next_wp, next_cmd = self._route_planner.run_step(pos)
		result['next_command'] = next_cmd.value


		theta = compass + np.pi/2
		R = np.array([
			[np.cos(theta), -np.sin(theta)],
			[np.sin(theta), np.cos(theta)]
			])

		local_command_point = np.array([next_wp[0]-pos[0], next_wp[1]-pos[1]])
		local_command_point = R.T.dot(local_command_point)
		result['target_point'] = tuple(local_command_point)

		# jxy addition:
		result['far_command'] = next_cmd

		result['R_pos_from_head'] = R
		result['offset_pos'] = np.array([pos[0], pos[1]])
		# from team_code/map_agent.py:
		self._actors = self._world.get_actors()
		self._traffic_lights = get_nearby_lights(self._vehicle, self._actors.filter('*traffic_light*'))
		topdown = input_data['map'][1][:, :, 2]
		topdown = draw_traffic_lights(topdown, self._vehicle, self._traffic_lights)
		result['topdown'] = COLOR[CONVERTER[topdown]]
		return result

	@torch.no_grad()
	def run_step(self, input_data, timestamp):
		if not self.initialized:
			self._init()
		tick_data = self.tick(input_data)
		if self.step < self.config.seq_len:
			rgb = self._im_transform(tick_data['rgb']).unsqueeze(0)

			control = carla.VehicleControl()
			control.steer = 0.0
			control.throttle = 0.0
			control.brake = 0.0
			
			self.record_step(tick_data, control) # jxy: add
			return control

		gt_velocity = torch.FloatTensor([tick_data['speed']]).to('cuda', dtype=torch.float32)
		command = tick_data['next_command']
		if command < 0:
			command = 4
		command -= 1
		assert command in [0, 1, 2, 3, 4, 5]
		cmd_one_hot = [0] * 6
		cmd_one_hot[command] = 1
		cmd_one_hot = torch.tensor(cmd_one_hot).view(1, 6).to('cuda', dtype=torch.float32)
		speed = torch.FloatTensor([float(tick_data['speed'])]).view(1,1).to('cuda', dtype=torch.float32)
		speed = speed / 12
		rgb = self._im_transform(tick_data['rgb']).unsqueeze(0).to('cuda', dtype=torch.float32)

		tick_data['target_point'] = [torch.FloatTensor([tick_data['target_point'][0]]),
										torch.FloatTensor([tick_data['target_point'][1]])]
		target_point = torch.stack(tick_data['target_point'], dim=1).to('cuda', dtype=torch.float32)
		state = torch.cat([speed, target_point, cmd_one_hot], 1)

		pred= self.net(rgb, state, target_point)

		steer_ctrl, throttle_ctrl, brake_ctrl, metadata = self.net.process_action(pred, tick_data['next_command'], gt_velocity, target_point)

		 # jxy: points_world
		steer_traj, throttle_traj, brake_traj, metadata_traj, points_world = self.net.control_pid(pred['pred_wp'], gt_velocity, target_point)
		if brake_traj < 0.05: brake_traj = 0.0
		if throttle_traj > brake_traj: brake_traj = 0.0

		self.pid_metadata = metadata_traj
		control = carla.VehicleControl()

		if self.status == 0:
			self.alpha = 0.3
			self.pid_metadata['agent'] = 'traj'
			control.steer = np.clip(self.alpha*steer_ctrl + (1-self.alpha)*steer_traj, -1, 1)
			control.throttle = np.clip(self.alpha*throttle_ctrl + (1-self.alpha)*throttle_traj, 0, 0.75)
			control.brake = np.clip(self.alpha*brake_ctrl + (1-self.alpha)*brake_traj, 0, 1)
		else:
			self.alpha = 0.3
			self.pid_metadata['agent'] = 'ctrl'
			control.steer = np.clip(self.alpha*steer_traj + (1-self.alpha)*steer_ctrl, -1, 1)
			control.throttle = np.clip(self.alpha*throttle_traj + (1-self.alpha)*throttle_ctrl, 0, 0.75)
			control.brake = np.clip(self.alpha*brake_traj + (1-self.alpha)*brake_ctrl, 0, 1)


		self.pid_metadata['steer_ctrl'] = float(steer_ctrl)
		self.pid_metadata['steer_traj'] = float(steer_traj)
		self.pid_metadata['throttle_ctrl'] = float(throttle_ctrl)
		self.pid_metadata['throttle_traj'] = float(throttle_traj)
		self.pid_metadata['brake_ctrl'] = float(brake_ctrl)
		self.pid_metadata['brake_traj'] = float(brake_traj)

		if control.brake > 0.5:
			control.throttle = float(0)

		if len(self.last_steers) >= 20:
			self.last_steers.popleft()
		self.last_steers.append(abs(float(control.steer)))
		#chech whether ego is turning
		# num of steers larger than 0.1
		num = 0
		for s in self.last_steers:
			if s > 0.10:
				num += 1
		if num > 10:
			self.status = 1
			self.steer_step += 1

		else:
			self.status = 0

		self.pid_metadata['status'] = self.status

		if HAS_DISPLAY: # jxy: change
			debug_display(tick_data, control.steer, control.throttle, control.brake, self.step)

		self.record_step(tick_data, control, points_world) # jxy: add
		return control

	# jxy: add record_step
	def record_step(self, tick_data, control, pred_waypoint=[]):
		# draw pred_waypoint
		if len(pred_waypoint):
			pred_waypoint[:,1] *= -1
			pred_waypoint = tick_data['R_pos_from_head'].dot(pred_waypoint.T).T
		self._route_planner.run_step2(pred_waypoint, is_gps=False, store=False) # metadata['wp_1'] relative to ego head (as y)
		# addition: from leaderboard/team_code/auto_pilot.py
		speed = tick_data['speed']
		self._recorder_tick(control) # trjs
		ego_bbox = self.gather_info() # metrics
		self._route_planner.run_step2(ego_bbox + tick_data['offset_pos'], is_gps=True, store=False)
		self._route_planner.show_route()

		if self.save_path is not None and self.step % self.record_every_n_step == 0:
			self.save(control.steer, control.throttle, control.brake, tick_data)


# jxy: mv save in AgentSaver & rm destroy
class AgentSaver(Saver):
	def __init__(self, path_to_conf_file, dict_, list_):
		self.config_path = path_to_conf_file

		# jxy: according to sensor
		self.rgb_list = ['rgb', 'topdown', ] # 'bev', 
		self.add_img = [] # 'flow', 'out', 
		self.lidar_list = [] # 'lidar_0', 'lidar_1',
		self.dir_names = self.rgb_list + self.add_img + self.lidar_list + ['pid_metadata']

		super().__init__(dict_, list_)

	def run(self): # jxy: according to init_ads
		self.config = GlobalConfig()

		super().run()

	def _save(self, tick_data):	
		# addition
		# save_action_based_measurements = tick_data['save_action_based_measurements']
		self.save_path = tick_data['save_path']
		if not (self.save_path / 'ADS_log.csv' ).exists():
			# addition: generate dir for every total_i
			self.save_path.mkdir(parents=True, exist_ok=True)
			for dir_name in self.dir_names:
				(self.save_path / dir_name).mkdir(parents=True, exist_ok=False)

			# according to self.save data_row_list
			title_row = ','.join(
				['frame_id', 'far_command', 'speed', 'steering', 'throttle', 'brake',] + \
				self.dir_names
			)
			with (self.save_path / 'ADS_log.csv' ).open("a") as f_out:
				f_out.write(title_row+'\n')

		self.step = tick_data['frame']
		self.save(tick_data['steer'],tick_data['throttle'],tick_data['brake'], tick_data)

	# addition: modified from leaderboard/team_code/auto_pilot.py
	def save(self, steer, throttle, brake, tick_data):
		# frame = self.step // 10
		frame = self.step

		# 'gps' 'thetas'
		pos = tick_data['gps']
		speed = tick_data['speed']
		far_command = tick_data['far_command']
		data_row_list = [frame, far_command.name, speed, steer, throttle, brake,]

		if frame >= self.config.seq_len: # jxy: according to run_step
			# images
			for rgb_name in self.rgb_list + self.add_img:
				path_ = self.save_path / rgb_name / ('%04d.png' % frame)
				Image.fromarray(tick_data[rgb_name]).save(path_)
				data_row_list.append(str(path_))
			# lidar
			for i, rgb_name in enumerate(self.lidar_list):
				path_ = self.save_path / rgb_name / ('%04d.png' % frame)
				Image.fromarray(cm.gist_earth(tick_data['lidar_processed'][0][0, i], bytes=True)).save(path_)
				data_row_list.append(str(path_))

			# pid_metadata
			pid_metadata = tick_data['pid_metadata']
			path_ = self.save_path / 'pid_metadata' / ('%04d.json' % frame)
			outfile = open(path_, 'w')
			json.dump(pid_metadata, outfile, indent=4)
			outfile.close()
			data_row_list.append(str(path_))

		# collection
		data_row = ','.join([str(i) for i in data_row_list])
		with (self.save_path / 'ADS_log.csv' ).open("a") as f_out:
			f_out.write(data_row+'\n')


	# def save(self, tick_data):
	# 	frame = self.step // 10

	# 	Image.fromarray(tick_data['rgb']).save(self.save_path / 'rgb' / ('%04d.png' % frame))

	# 	Image.fromarray(tick_data['bev']).save(self.save_path / 'bev' / ('%04d.png' % frame))

	# 	outfile = open(self.save_path / 'meta' / ('%04d.json' % frame), 'w')
	# 	json.dump(self.pid_metadata, outfile, indent=4)
	# 	outfile.close()

	# def destroy(self):
	# 	del self.net
	# 	torch.cuda.empty_cache()