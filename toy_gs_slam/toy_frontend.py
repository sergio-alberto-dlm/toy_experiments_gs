import time

import numpy as np
import torch
import torch.multiprocessing as mp

from tiny_renderer.gaussian_2D_render import surface_splatting
from MonoGS.gaussian_splatting.utils.graphics_utils import getProjectionMatrix2, getWorld2View2
from MonoGS.utils.camera_utils import Camera
from MonoGS.utils.eval_utils import eval_ate, save_gaussians
from MonoGS.utils.logging_utils import Log
from MonoGS.utils.pose_utils import update_pose
from MonoGS.utils.slam_utils import get_loss_tracking, get_median_depth


class FrontEnd(mp.Process):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.frontend_queue = None

        self.initialized = False
        self.monocular = config["Training"]["monocular"]
        self.iteration_count = 0
        self.current_window = []

        self.reset = True
        self.requested_init = False
        self.use_every_n_frames = 1

        self.gaussians = None   #<-------------------- here I am going to store the gaussian params for the tiny parameter 
        self.cameras = dict()
        self.device = "cpu" # "cuda:0"
        self.pause = False

    def set_hyperparams(self):
        self.save_dir = self.config["Results"]["save_dir"]
        self.save_results = self.config["Results"]["save_results"]
        self.save_trj = self.config["Results"]["save_trj"]
        self.tracking_itr_num = self.config["Training"]["tracking_itr_num"]

    def initialize(self, cur_frame_idx, viewpoint):
        self.initialized = not self.monocular
        self.iteration_count = 0
        self.current_window = []

        viewpoint.update_RT(viewpoint.R_gt, viewpoint.T_gt)
        self.reset = False

    def tracking(self, cur_frame_idx, viewpoint):
        prev = self.cameras[cur_frame_idx - self.use_every_n_frames]
        viewpoint.update_RT(prev.R, prev.T)

        opt_params = []
        opt_params.append(
            {
                "params": [viewpoint.cam_rot_delta],
                "lr": self.config["Training"]["lr"]["cam_rot_delta"],
                "name": "rot_{}".format(viewpoint.uid),
            }
        )
        opt_params.append(
            {
                "params": [viewpoint.cam_trans_delta],
                "lr": self.config["Training"]["lr"]["cam_trans_delta"],
                "name": "trans_{}".format(viewpoint.uid),
            }
        )
        opt_params.append(
            {
                "params": [viewpoint.exposure_a],
                "lr": 0.01,
                "name": "exposure_a_{}".format(viewpoint.uid),
            }
        )
        opt_params.append(
            {
                "params": [viewpoint.exposure_b],
                "lr": 0.01,
                "name": "exposure_b_{}".format(viewpoint.uid),
            }
        )

        pose_optimizer = torch.optim.Adam(opt_params)
        for tracking_itr in range(self.tracking_itr_num):
#------------------------------------------------------------------->
            render_2D = surface_splatting(
                self.gaussians.means3D, self.scales, 
                self.gaussians.quats, viewpoint, 
                self.gaussians.projmat, self.gaussians.colors, 
                self.gaussians.opacities, self.gaussians.intrins, 
                device=self.device
            )
            image, depth, opacity = (
                render_2D["render"], 
                render_2D["depth"], 
                render_2D["opacity"]
            )
#------------------------------------------------------------------->
            pose_optimizer.zero_grad()
            loss_tracking = get_loss_tracking(
                self.config, image, depth, opacity, viewpoint
            )
            loss_tracking.backward()

            with torch.no_grad():
                pose_optimizer.step()
                converged = update_pose(viewpoint)

            if converged:
                break

        self.median_depth = get_median_depth(depth, opacity)
        return render_2D 


    def run(self):
        cur_frame_idx = 0
        projection_matrix = getProjectionMatrix2(
            znear=0.01,
            zfar=100.0,
            fx=self.dataset.fx,
            fy=self.dataset.fy,
            cx=self.dataset.cx,
            cy=self.dataset.cy,
            W=self.dataset.width,
            H=self.dataset.height,
        ).transpose(0, 1)
        projection_matrix = projection_matrix.to(device=self.device)
        tic = torch.cuda.Event(enable_timing=True)
        toc = torch.cuda.Event(enable_timing=True)

        while True:

            if self.frontend_queue.empty():
                tic.record()
                if cur_frame_idx >= len(self.dataset):
                    if self.save_results:
                        eval_ate(
                            self.cameras,
                            range(len(self.dataset)), #self.kf_indices, <----------------------
                            self.save_dir,
                            0,
                            final=True,
                            monocular=self.monocular,
                        )
                    break

                viewpoint = Camera.init_from_dataset(
                    self.dataset, cur_frame_idx, projection_matrix
                )
                viewpoint.compute_grad_mask(self.config)

                self.cameras[cur_frame_idx] = viewpoint

                if self.reset:
                    self.initialize(cur_frame_idx, viewpoint)
                    self.current_window.append(cur_frame_idx)
                    cur_frame_idx += 1
                    continue

                self.initialized = self.initialized or (
                    len(self.current_window) == self.window_size
                )

                # Tracking
                render_pkg = self.tracking(cur_frame_idx, viewpoint)

                cur_frame_idx += 1

                if (
                    cur_frame_idx % 200 == 0
                ):
                    Log("Evaluating ATE at frame: ", cur_frame_idx)
                    eval_ate(
                        self.cameras,
                        self.kf_indices,
                        self.save_dir,
                        cur_frame_idx,
                        monocular=self.monocular,
                    )
                toc.record()
                # torch.cuda.synchronize()
            else:
                print("Error with the fronend queue")