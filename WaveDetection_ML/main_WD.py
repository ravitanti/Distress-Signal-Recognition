import argparse
import time
import cv2
import numpy as np
import torch

from models.with_mobilenet import PoseEstimationWithMobileNet
from modules.keypoints import extract_keypoints, group_keypoints
from modules.load_state import load_state
from modules.pose import Pose, track_poses
from val import normalize, pad_width

from ML.dataExtraction_WD import extract_data, save_to_csv, calculate_angle, infer, save_to_numpy,wave_detection,multi_person_distress

# from ..WaveDetection_algorithm.WaveDetection.functions import wave_detection


class ImageReader(object):
    def __init__(self, file_names):
        self.file_names = file_names
        self.max_idx = len(file_names)

    def __iter__(self):
        self.idx = 0
        return self

    def __next__(self):
        if self.idx == self.max_idx:
            raise StopIteration
        img = cv2.imread(self.file_names[self.idx], cv2.IMREAD_COLOR)
        if img.size == 0:
            raise IOError('Image {} cannot be read'.format(self.file_names[self.idx]))
        self.idx = self.idx + 1
        return img


class VideoReader(object):
    def __init__(self, file_name):
        self.file_name = file_name
        try:  # OpenCV needs int to read from webcam
            self.file_name = int(file_name)
        except ValueError:
            pass

    def __iter__(self):
        self.cap = cv2.VideoCapture(self.file_name)
        if not self.cap.isOpened():
            raise IOError('Video {} cannot be opened'.format(self.file_name))
        return self

    def __next__(self):
        was_read, img = self.cap.read()
        if not was_read:
            raise StopIteration
        return img


def infer_fast(net, img, net_input_height_size, stride, upsample_ratio, cpu,
               pad_value=(0, 0, 0), img_mean=np.array([128, 128, 128], np.float32), img_scale=np.float32(1/256)):
    height, width, _ = img.shape
    scale = net_input_height_size / height
    
    scaled_img = cv2.resize(img, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    scaled_img = normalize(scaled_img, img_mean, img_scale)
    min_dims = [net_input_height_size, max(scaled_img.shape[1], net_input_height_size)]
    padded_img, pad = pad_width(scaled_img, stride, pad_value, min_dims)

    tensor_img = torch.from_numpy(padded_img).permute(2, 0, 1).unsqueeze(0).float()
    if not cpu:
        tensor_img = tensor_img.cuda()

    stages_output = net(tensor_img)

    stage2_heatmaps = stages_output[-2]
    heatmaps = np.transpose(stage2_heatmaps.squeeze().cpu().data.numpy(), (1, 2, 0))
    heatmaps = cv2.resize(heatmaps, (0, 0), fx=upsample_ratio, fy=upsample_ratio, interpolation=cv2.INTER_CUBIC)

    stage2_pafs = stages_output[-1]
    pafs = np.transpose(stage2_pafs.squeeze().cpu().data.numpy(), (1, 2, 0))
    pafs = cv2.resize(pafs, (0, 0), fx=upsample_ratio, fy=upsample_ratio, interpolation=cv2.INTER_CUBIC)

    return heatmaps, pafs, scale, pad


def run_demo(net, image_provider, height_size, cpu, track, smooth):
    net = net.eval()
    if not cpu:
        net = net.cuda()

    stride = 8
    upsample_ratio = 4
    num_keypoints = Pose.num_kpts
    previous_poses = []
    delay = 1
    new_frame_time = 0 
    prev_frame_time = 0 
    trainData = None
    waveDetection = 0
    


    #initializing for algorihtm
    waveCounter = 0
    state = None # NONE |  open or close - > 0 0r 1
    
    for img in image_provider:

        orig_img = img.copy()
        heatmaps, pafs, scale, pad = infer_fast(net, img, height_size, stride, upsample_ratio, cpu)

        total_keypoints_num = 0
        all_keypoints_by_type = []
        for kpt_idx in range(num_keypoints):  # 19th for bg
            total_keypoints_num += extract_keypoints(heatmaps[:, :, kpt_idx], all_keypoints_by_type, total_keypoints_num)

        pose_entries, all_keypoints = group_keypoints(all_keypoints_by_type, pafs)
        for kpt_id in range(all_keypoints.shape[0]):
            all_keypoints[kpt_id, 0] = (all_keypoints[kpt_id, 0] * stride / upsample_ratio - pad[1]) / scale
            all_keypoints[kpt_id, 1] = (all_keypoints[kpt_id, 1] * stride / upsample_ratio - pad[0]) / scale
        current_poses = []
        for n in range(len(pose_entries)):
            if len(pose_entries[n]) == 0:
                continue
            pose_keypoints = np.ones((num_keypoints, 2), dtype=np.int32) * -1
            for kpt_id in range(num_keypoints):
                if pose_entries[n][kpt_id] != -1.0:  # keypoint was found
                    pose_keypoints[kpt_id, 0] = int(all_keypoints[int(pose_entries[n][kpt_id]), 0])
                    pose_keypoints[kpt_id, 1] = int(all_keypoints[int(pose_entries[n][kpt_id]), 1])
            pose = Pose(pose_keypoints, pose_entries[n][18])
            current_poses.append(pose)


        font = cv2.FONT_HERSHEY_SIMPLEX   # font
        fontScale = 0.8 #fontScale
        color = (255, 255, 255)
        thickness = 2

        new_frame_time = time.time()
 # Calculating the fps
        # fps will be number of frame processed in given time frame
        # since their will be most of time error of 0.001 second
        fps = 1/(new_frame_time-prev_frame_time)  # we will be subtracting it to get more accurate result
        fps = int(fps) # converting the fps into integer
        cv2.putText(img, 'FPS: '+ str(fps), (500,20), font, 
                        fontScale,(140,11,4), thickness, cv2.LINE_4)
        
        prev_frame_time = new_frame_time
        

        if track:
            track_poses(previous_poses, current_poses, smooth=smooth)
            previous_poses = current_poses
        for pose in current_poses:  # drawing pose
            pose.draw(img)
        img = cv2.addWeighted(orig_img, 0.6, img, 0.4, 0)
    
        for pose in current_poses:

     
             ### show keypoints on display
            #draw coordinates on frame
            # cv2.putText(img, "R", (right_shoulderY,right_shoulderX ), font, fontScale, # Righ side inidcator
            #             color, thickness, cv2.LINE_4)
            
            # cv2.rectangle(img, (pose.bbox[0], pose.bbox[1]),
            #               (pose.bbox[0] + pose.bbox[2], pose.bbox[1] + pose.bbox[3]), (0, 255, 0))
            if track:
                cv2.putText(img, 'id: {}'.format(pose.id), (pose.bbox[0], pose.bbox[1] - 16),
                            cv2.FONT_HERSHEY_COMPLEX, 0.5, (0, 0, 255))


        # order of the [y,x] pose.keypoints => [0'nose', 1'neck', 2'r_sho', 3'r_elb', 4'r_wri', 5'l_sho', 6'l_elb',7'l_wri', 8'r_hip', 
        #                            9'r_knee', 10'r_ank', 11'l_hip', 12'l_knee', 13'l_ank', 14'r_eye', 15'l_eye', 16'r_ear', 17'l_ear']
                # coordinateY = pose.keypoints[0][0] # sample - getting nose key points y coordinate
                # coordinateX = pose.keypoints[0][1] # sample - getting nose key points x coordinate 

        ### algorithmic wave
            # waveCounter, state, wave = wave_detection(pose.keypoints,waveCounter,state) # hand wave detection algorithm
            # if wave:
            #     color = (0, 0, 255)
            #     cv2.putText(img, "Algo: ", (10, 25), font, fontScale,(255,255,255), thickness, cv2.LINE_4)
            #     cv2.putText(img, " Distress signal detected", (60, 25), font, fontScale,color, thickness, cv2.LINE_4)
            # else:
            #     color = (27, 140, 4)
            #     cv2.putText(img, "Algo: ", (10, 25), font, fontScale,(255,255,255), thickness, cv2.LINE_4)
            #     # cv2.putText(img, " No distress signal", (60, 25), font, fontScale,color, thickness, cv2.LINE_4)
            
        ## collect training data
            trainData = extract_data(pose.keypoints,False)
        
        ### prediction single pperson
            #waveDetection = infer(pose.keypoints)

        # ### multiperson 
        #     waveDetection = multi_person_distress(pose.id, pose.keypoints)
        #     if waveDetection >=80:
        #         break
        
            ############ just for refrence
            right_shoulderY,right_shoulderX =  pose.keypoints[2][0], pose.keypoints[2][1]
            left_shoulderY, left_shoulderX =  pose.keypoints[5][0], pose.keypoints[5][1]
            right_wrist,left_wrist = pose.keypoints[4], pose.keypoints[7]
            cv2.line(img, (right_shoulderY,right_shoulderX), (left_shoulderY, left_shoulderX), color, thickness)
            cv2.line(img, (right_shoulderY,right_shoulderX), right_wrist, color, thickness) 
            cv2.line(img, (left_shoulderY, left_shoulderX), left_wrist, color, thickness) 
            ################  

        # if waveDetection >=80:
        #     color = (0, 0, 255)
        #     cv2.putText(img, "ML: ", (10, 60), font, fontScale,(255,255,255), thickness, cv2.LINE_4)
        #     cv2.putText(img, "Distress signal detected " +str(waveDetection) + " %", (60, 60), font, fontScale,color, thickness, cv2.LINE_4)
        # else:
        #     color = (27, 140, 4)
        #     cv2.putText(img, "ML: ", (10, 60), font, fontScale,(255,255,255), thickness, cv2.LINE_4)
        #     # cv2.putText(img, "No Distress signal detected " +str(waveDetection) + " %", (60, 60), font, fontScale,color, thickness, cv2.LINE_4)
           


        cv2.imshow('Distress Signal recognition', img)


        key = cv2.waitKey(delay)
        if key == 27:  # esc
            if trainData:
                #save_to_csv(trainData)
                save_to_numpy(trainData)
                print("train data saved")
            else:
                print("No Train data")
            return
        elif key == 112:  # 'p'
            
            if delay == 1:
                delay = 0
            else:
                delay = 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='''Lightweight human pose estimation python demo.
                       This is just for quick results preview.
                       Please, consider c++ demo for the best performance.''')
    parser.add_argument('--checkpoint-path', type=str, required=True, help='path to the checkpoint')
    parser.add_argument('--height-size', type=int, default=256, help='network input layer height size')
    parser.add_argument('--video', type=str, default='', help='path to video file or camera id')
    parser.add_argument('--images', nargs='+', default='', help='path to input image(s)')
    parser.add_argument('--cpu', action='store_true', help='run network inference on cpu')
    parser.add_argument('--track', type=int, default=1, help='track pose id in video')
    parser.add_argument('--smooth', type=int, default=1, help='smooth pose keypoints')
    args = parser.parse_args()

    if args.video == '' and args.images == '':
        raise ValueError('Either --video or --image has to be provided')

    net = PoseEstimationWithMobileNet()
    checkpoint = torch.load(args.checkpoint_path, map_location='cpu')
    load_state(net, checkpoint)

    frame_provider = ImageReader(args.images)
    if args.video != '':
        frame_provider = VideoReader(args.video)
    else:
        args.track = 0

    run_demo(net, frame_provider, args.height_size, args.cpu, args.track, args.smooth)
