# Copyright (c) 2018 Andy Zeng

import numpy as np

from numba import njit, prange
from skimage import measure

try:
  import pycuda.driver as cuda
  import pycuda.autoinit
  from pycuda.compiler import SourceModule
  FUSION_GPU_MODE = 1
except Exception as err:
  print('Warning: {}'.format(err))
  print('Failed to import PyCUDA. Running fusion in CPU mode.')
  FUSION_GPU_MODE = 0


class TSDFVolume:
  """Volumetric TSDF Fusion of RGB-D Images.
  """
  def reset_visible(self):
    # self._tsdf_vol_cpu[:] = 1.0
    # self._occl_vol_cpu = -100*np.ones(self._vol_dim).astype(np.float32)
    self._weight_vol_cpu[:] = 0.0
    # self._color_vol_cpu = np.zeros(self._vol_dim).astype(np.float32)
    self._mask_vol_cpu[:] = 0.0

    # Copy voxel volumes to GPU
    if self.gpu_mode:
      # cuda.memcpy_htod(self._tsdf_vol_gpu,self._tsdf_vol_cpu)
      # cuda.memcpy_htod(self._occl_vol_gpu,self._occl_vol_cpu)
      cuda.memcpy_htod(self._weight_vol_gpu,self._weight_vol_cpu)
      # cuda.memcpy_htod(self._color_vol_gpu,self._color_vol_cpu)
      cuda.memcpy_htod(self._mask_vol_gpu,self._mask_vol_cpu)


  def __init__(self, vol_bnds, voxel_size, use_gpu=True):
    """Constructor.

    Args:
      vol_bnds (ndarray): An ndarray of shape (3, 2). Specifies the
        xyz bounds (min/max) in meters.
      voxel_size (float): The volume discretization in meters.
    """
    vol_bnds = np.asarray(vol_bnds)
    assert vol_bnds.shape == (3, 2), "[!] `vol_bnds` should be of shape (3, 2)."

    # Define voxel volume parameters
    self._vol_bnds = vol_bnds
    self._voxel_size = float(voxel_size)
    self._trunc_margin = 5 * self._voxel_size  # truncation on SDF
    print("trunc margin", self._trunc_margin)
    self._color_const = 256 * 256

    # Adjust volume bounds and ensure C-order contiguous
    self._vol_dim = np.ceil((self._vol_bnds[:,1]-self._vol_bnds[:,0])/self._voxel_size).copy(order='C').astype(int)
    self._vol_bnds[:,1] = self._vol_bnds[:,0]+self._vol_dim*self._voxel_size
    self._vol_origin = self._vol_bnds[:,0].copy(order='C').astype(np.float32)

    print("Voxel volume size: {} x {} x {} - # points: {:,}".format(
      self._vol_dim[0], self._vol_dim[1], self._vol_dim[2],
      self._vol_dim[0]*self._vol_dim[1]*self._vol_dim[2])
    )

    # Initialize pointers to voxel volume in CPU memory
    self._tsdf_vol_cpu = np.ones(self._vol_dim).astype(np.float32)
    self._occl_vol_cpu = -100*np.ones(self._vol_dim).astype(np.float32)
    # for computing the cumulative moving average of observations per voxel
    self._weight_vol_cpu = np.zeros(self._vol_dim).astype(np.float32)
    self._color_vol_cpu = np.zeros(self._vol_dim).astype(np.float32)
    self._mask_vol_cpu = np.zeros(self._vol_dim).astype(np.uint32)

    self.gpu_mode = use_gpu and FUSION_GPU_MODE

    # Copy voxel volumes to GPU
    if self.gpu_mode:
      self._tsdf_vol_gpu = cuda.mem_alloc(self._tsdf_vol_cpu.nbytes)
      cuda.memcpy_htod(self._tsdf_vol_gpu,self._tsdf_vol_cpu)
      self._occl_vol_gpu = cuda.mem_alloc(self._occl_vol_cpu.nbytes)
      cuda.memcpy_htod(self._occl_vol_gpu,self._occl_vol_cpu)
      self._weight_vol_gpu = cuda.mem_alloc(self._weight_vol_cpu.nbytes)
      cuda.memcpy_htod(self._weight_vol_gpu,self._weight_vol_cpu)
      self._color_vol_gpu = cuda.mem_alloc(self._color_vol_cpu.nbytes)
      cuda.memcpy_htod(self._color_vol_gpu,self._color_vol_cpu)
      self._mask_vol_gpu = cuda.mem_alloc(self._mask_vol_cpu.nbytes)
      cuda.memcpy_htod(self._mask_vol_gpu,self._mask_vol_cpu)

      # Cuda kernel function (C++)
      self._cuda_src_mod = SourceModule("""
        __global__ void integrate(float * tsdf_vol,
                                  float * occl_vol,
                                  float * weight_vol,
                                  float * color_vol,
                                  unsigned int * mask_vol,
                                  float * vol_dim,
                                  float * vol_origin,
                                  float * cam_intr,
                                  float * cam_pose,
                                  float * rgb_intr,
                                  float * rgb_pose,
                                  float * other_params,
                                  float * color_im,
                                  float * depth_im,
                                  unsigned int * mask_im) {
          // Get voxel index
          int gpu_loop_idx = (int) other_params[0];
          int max_threads_per_block = blockDim.x;
          int block_idx = blockIdx.z*gridDim.y*gridDim.x+blockIdx.y*gridDim.x+blockIdx.x;
          int voxel_idx = gpu_loop_idx*gridDim.x*gridDim.y*gridDim.z*max_threads_per_block+block_idx*max_threads_per_block+threadIdx.x;
          int vol_dim_x = (int) vol_dim[0];
          int vol_dim_y = (int) vol_dim[1];
          int vol_dim_z = (int) vol_dim[2];
          if (voxel_idx > vol_dim_x*vol_dim_y*vol_dim_z)
              return;
          // Get voxel grid coordinates (note: be careful when casting)
          float voxel_x = floorf(((float)voxel_idx)/((float)(vol_dim_y*vol_dim_z)));
          float voxel_y = floorf(((float)(voxel_idx-((int)voxel_x)*vol_dim_y*vol_dim_z))/((float)vol_dim_z));
          float voxel_z = (float)(voxel_idx-((int)voxel_x)*vol_dim_y*vol_dim_z-((int)voxel_y)*vol_dim_z);
          // Voxel grid coordinates to world coordinates
          float voxel_size = other_params[1];
          float pt_x = vol_origin[0]+voxel_x*voxel_size;
          float pt_y = vol_origin[1]+voxel_y*voxel_size;
          float pt_z = vol_origin[2]+voxel_z*voxel_size;
          // World coordinates to camera coordinates
          float tmp_pt_x = pt_x-cam_pose[0*4+3];
          float tmp_pt_y = pt_y-cam_pose[1*4+3];
          float tmp_pt_z = pt_z-cam_pose[2*4+3];
          float cam_pt_x = cam_pose[0*4+0]*tmp_pt_x+cam_pose[1*4+0]*tmp_pt_y+cam_pose[2*4+0]*tmp_pt_z;
          float cam_pt_y = cam_pose[0*4+1]*tmp_pt_x+cam_pose[1*4+1]*tmp_pt_y+cam_pose[2*4+1]*tmp_pt_z;
          float cam_pt_z = cam_pose[0*4+2]*tmp_pt_x+cam_pose[1*4+2]*tmp_pt_y+cam_pose[2*4+2]*tmp_pt_z;
          // Camera coordinates to image pixels
          int pixel_x = (int) roundf(cam_intr[0*3+0]*(cam_pt_x/cam_pt_z)+cam_intr[0*3+2]);
          int pixel_y = (int) roundf(cam_intr[1*3+1]*(cam_pt_y/cam_pt_z)+cam_intr[1*3+2]);
          // Skip if outside view frustum
          int im_h = (int) other_params[2];
          int im_w = (int) other_params[3];
          if (pixel_x < 0 || pixel_x >= im_w || pixel_y < 0 || pixel_y >= im_h || cam_pt_z<0)
              return;
          // Skip invalid depth
          float depth_value = depth_im[pixel_y*im_w+pixel_x];
          if (depth_value == 0)
              return;
          // Integrate mask
          unsigned int old_mask = mask_vol[voxel_idx];
          unsigned int new_mask = mask_im[pixel_y*im_w+pixel_x];
          mask_vol[voxel_idx] = old_mask | new_mask;
          // Integrate TSDF and Occlusion
          float trunc_margin = other_params[4];
          float depth_diff = depth_value-cam_pt_z;
          occl_vol[voxel_idx] = fmax(occl_vol[voxel_idx],depth_diff);
          if (depth_diff < -trunc_margin)
              return;
          float dist = fmin(1.0f,depth_diff/trunc_margin);
          float w_old = weight_vol[voxel_idx];
          float obs_weight = other_params[5];
          float w_new = w_old + obs_weight;
          weight_vol[voxel_idx] = w_new;
          tsdf_vol[voxel_idx] = (tsdf_vol[voxel_idx]*w_old+obs_weight*dist)/w_new;
          // World coordinates to rgb camera coordinates
          tmp_pt_x = pt_x-rgb_pose[0*4+3];
          tmp_pt_y = pt_y-rgb_pose[1*4+3];
          tmp_pt_z = pt_z-rgb_pose[2*4+3];
          float rgb_pt_x = rgb_pose[0*4+0]*tmp_pt_x+rgb_pose[1*4+0]*tmp_pt_y+rgb_pose[2*4+0]*tmp_pt_z;
          float rgb_pt_y = rgb_pose[0*4+1]*tmp_pt_x+rgb_pose[1*4+1]*tmp_pt_y+rgb_pose[2*4+1]*tmp_pt_z;
          float rgb_pt_z = rgb_pose[0*4+2]*tmp_pt_x+rgb_pose[1*4+2]*tmp_pt_y+rgb_pose[2*4+2]*tmp_pt_z;
          // Camera coordinates to image pixels
          pixel_x = (int) roundf(rgb_intr[0*3+0]*(rgb_pt_x/rgb_pt_z)+rgb_intr[0*3+2]);
          pixel_y = (int) roundf(rgb_intr[1*3+1]*(rgb_pt_y/rgb_pt_z)+rgb_intr[1*3+2]);
          if (pixel_x < 0 || pixel_x >= im_w || pixel_y < 0 || pixel_y >= im_h || cam_pt_z<0)
              return;
          // Integrate color
          float old_color = color_vol[voxel_idx];
          float old_b = floorf(old_color/(256*256));
          float old_g = floorf((old_color-old_b*256*256)/256);
          float old_r = old_color-old_b*256*256-old_g*256;
          float new_color = color_im[pixel_y*im_w+pixel_x];
          float new_b = floorf(new_color/(256*256));
          float new_g = floorf((new_color-new_b*256*256)/256);
          float new_r = new_color-new_b*256*256-new_g*256;
          new_b = fmin(roundf((old_b*w_old+obs_weight*new_b)/w_new),255.0f);
          new_g = fmin(roundf((old_g*w_old+obs_weight*new_g)/w_new),255.0f);
          new_r = fmin(roundf((old_r*w_old+obs_weight*new_r)/w_new),255.0f);
          color_vol[voxel_idx] = new_b*256*256+new_g*256+new_r;
        }""")

      self._cuda_integrate = self._cuda_src_mod.get_function("integrate")

      # Determine block/grid size on GPU
      gpu_dev = cuda.Device(0)
      self._max_gpu_threads_per_block = gpu_dev.MAX_THREADS_PER_BLOCK
      n_blocks = int(np.ceil(float(np.prod(self._vol_dim))/float(self._max_gpu_threads_per_block)))
      grid_dim_x = min(gpu_dev.MAX_GRID_DIM_X,int(np.floor(np.cbrt(n_blocks))))
      grid_dim_y = min(gpu_dev.MAX_GRID_DIM_Y,int(np.floor(np.sqrt(n_blocks/grid_dim_x))))
      grid_dim_z = min(gpu_dev.MAX_GRID_DIM_Z,int(np.ceil(float(n_blocks)/float(grid_dim_x*grid_dim_y))))
      self._max_gpu_grid_dim = np.array([grid_dim_x,grid_dim_y,grid_dim_z]).astype(int)
      self._n_gpu_loops = int(np.ceil(float(np.prod(self._vol_dim))/float(np.prod(self._max_gpu_grid_dim)*self._max_gpu_threads_per_block)))

    else:
      # Get voxel grid coordinates
      xv, yv, zv = np.meshgrid(
        range(self._vol_dim[0]),
        range(self._vol_dim[1]),
        range(self._vol_dim[2]),
        indexing='ij'
      )
      self.vox_coords = np.concatenate([
        xv.reshape(1,-1),
        yv.reshape(1,-1),
        zv.reshape(1,-1)
      ], axis=0).astype(int).T

  @staticmethod
  @njit(parallel=True)
  def vox2world(vol_origin, vox_coords, vox_size):
    """Convert voxel grid coordinates to world coordinates.
    """
    vol_origin = vol_origin.astype(np.float32)
    vox_coords = vox_coords.astype(np.float32)
    cam_pts = np.empty_like(vox_coords, dtype=np.float32)
    for i in prange(vox_coords.shape[0]):
      for j in range(3):
        cam_pts[i, j] = vol_origin[j] + (vox_size * vox_coords[i, j])
    return cam_pts

  @staticmethod
  @njit(parallel=True)
  def cam2pix(cam_pts, intr):
    """Convert camera coordinates to pixel coordinates.
    """
    intr = intr.astype(np.float32)
    fx, fy = intr[0, 0], intr[1, 1]
    cx, cy = intr[0, 2], intr[1, 2]
    pix = np.empty((cam_pts.shape[0], 2), dtype=np.int64)
    for i in prange(cam_pts.shape[0]):
      pix[i, 0] = int(np.round((cam_pts[i, 0] * fx / cam_pts[i, 2]) + cx))
      pix[i, 1] = int(np.round((cam_pts[i, 1] * fy / cam_pts[i, 2]) + cy))
    return pix

  @staticmethod
  @njit(parallel=True)
  def integrate_tsdf(tsdf_vol, occl_vol, dist, w_old, obs_weight):
    """Integrate the TSDF volume.
    """
    tsdf_vol_int = np.empty_like(tsdf_vol, dtype=np.float32)
    occl_vol_int = np.empty_like(occl_vol, dtype=np.float32)
    # w_new = np.empty_like(w_old, dtype=np.float32)
    for i in prange(len(tsdf_vol)):
      w_new[i] = w_old[i] + obs_weight
      tsdf_vol_int[i] = (w_old[i] * tsdf_vol[i] + obs_weight * dist[i]) / w_new[i]
      occl_vol_int[i] = max(occl_vol[i], dist[i])
    return tsdf_vol_int, w_new, occl_vol_int

  def integrate(self, color_im, depth_im, mask_im, cam_intr, cam_pose, rgb_intr, rgb_pose, obs_weight=1.):
    """Integrate an RGB-D frame into the TSDF volume.

    Args:
      color_im (ndarray): An RGB image of shape (H, W, 3).
      depth_im (ndarray): A depth image of shape (H, W).
      mask_im  (ndarray): A binary mask of shape (H, W).
      cam_intr (ndarray): The camera intrinsics matrix of shape (3, 3).
      cam_pose (ndarray): The camera pose (i.e. extrinsics) of shape (4, 4).
      rgb_intr (ndarray): The RGB camera intrinsics matrix of shape (3, 3).
      rgb_pose (ndarray): The RGB camera pose (i.e. extrinsics) of shape (4, 4).
      obs_weight (float): The weight to assign for the current observation. A higher
        value
    """
    im_h, im_w = depth_im.shape

    # Fold RGB color image into a single channel image
    color_im = color_im.astype(np.float32)
    color_im = np.floor(color_im[...,2]*self._color_const + color_im[...,1]*256 + color_im[...,0])

    if self.gpu_mode:  # GPU mode: integrate voxel volume (calls CUDA kernel)
      for gpu_loop_idx in range(self._n_gpu_loops):
        self._cuda_integrate(self._tsdf_vol_gpu,
                            self._occl_vol_gpu,
                            self._weight_vol_gpu,
                            self._color_vol_gpu,
                            self._mask_vol_gpu,
                            cuda.InOut(self._vol_dim.astype(np.float32)),
                            cuda.InOut(self._vol_origin.astype(np.float32)),
                            cuda.InOut(cam_intr.reshape(-1).astype(np.float32)),
                            cuda.InOut(cam_pose.reshape(-1).astype(np.float32)),
                            cuda.InOut(rgb_intr.reshape(-1).astype(np.float32)),
                            cuda.InOut(rgb_pose.reshape(-1).astype(np.float32)),
                            cuda.InOut(np.asarray([
                              gpu_loop_idx,
                              self._voxel_size,
                              im_h,
                              im_w,
                              self._trunc_margin,
                              obs_weight
                            ], np.float32)),
                            cuda.InOut(color_im.reshape(-1).astype(np.float32)),
                            cuda.InOut(depth_im.reshape(-1).astype(np.float32)),
                            cuda.InOut(mask_im.reshape(-1).astype(np.uint32)),
                            block=(self._max_gpu_threads_per_block,1,1),
                            grid=(
                              int(self._max_gpu_grid_dim[0]),
                              int(self._max_gpu_grid_dim[1]),
                              int(self._max_gpu_grid_dim[2]),
                            )
        )
    else:  # CPU mode: integrate voxel volume (vectorized implementation)
      # Convert voxel grid coordinates to pixel coordinates
      cam_pts = self.vox2world(self._vol_origin, self.vox_coords, self._voxel_size)
      cam_pts = rigid_transform(cam_pts, np.linalg.inv(cam_pose))
      pix_z = cam_pts[:, 2]
      pix = self.cam2pix(cam_pts, cam_intr)
      pix_x, pix_y = pix[:, 0], pix[:, 1]

      # Eliminate pixels outside view frustum
      valid_pix = np.logical_and(pix_x >= 0,
                  np.logical_and(pix_x < im_w,
                  np.logical_and(pix_y >= 0,
                  np.logical_and(pix_y < im_h,
                  pix_z > 0))))
      depth_val = np.zeros(pix_x.shape)
      depth_val[valid_pix] = depth_im[pix_y[valid_pix], pix_x[valid_pix]]

      # Integrate TSDF
      depth_diff = depth_val - pix_z
      # valid_pts = np.logical_and(depth_val > 0, depth_diff >= -self._trunc_margin)
      # dist = np.minimum(.5, depth_diff / self._trunc_margin)
      valid_pts = (depth_val != 0)
      dist = depth_diff / self._trunc_margin
      valid_vox_x = self.vox_coords[valid_pts, 0]
      valid_vox_y = self.vox_coords[valid_pts, 1]
      valid_vox_z = self.vox_coords[valid_pts, 2]
      w_old = self._weight_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z]
      tsdf_vals = self._tsdf_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z]
      occl_vals = self._occl_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z]
      valid_dist = dist[valid_pts]
      tsdf_vol_new, w_new, occl_vol_new = self.integrate_tsdf(tsdf_vals, occl_vals, valid_dist, w_old, obs_weight)
      self._weight_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z] = w_new
      self._tsdf_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z] = tsdf_vol_new
      self._occl_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z] = occl_vol_new

      # get pixels map for rgb image
      rgb_pts = self.vox2world(self._vol_origin, self.vox_coords, self._voxel_size)
      rgb_pts = rigid_transform(rgb_pts, np.linalg.inv(rgb_pose))
      # pix_z = rgb_pts[:, 2]
      pix = self.cam2pix(rgb_pts, rgb_intr)
      pix_x, pix_y = pix[:, 0], pix[:, 1]

      # Integrate color
      old_color = self._color_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z]
      old_b = np.floor(old_color / self._color_const)
      old_g = np.floor((old_color-old_b*self._color_const)/256)
      old_r = old_color - old_b*self._color_const - old_g*256
      new_color = color_im[pix_y[valid_pts],pix_x[valid_pts]]
      new_b = np.floor(new_color / self._color_const)
      new_g = np.floor((new_color - new_b*self._color_const) /256)
      new_r = new_color - new_b*self._color_const - new_g*256
      new_b = np.minimum(255., np.round((w_old*old_b + obs_weight*new_b) / w_new))
      new_g = np.minimum(255., np.round((w_old*old_g + obs_weight*new_g) / w_new))
      new_r = np.minimum(255., np.round((w_old*old_r + obs_weight*new_r) / w_new))
      self._color_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z] = new_b*self._color_const + new_g*256 + new_r

      # Integrate mask
      old_mask = self._mask_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z]
      new_mask = mask_im[pix_y[valid_pts], pix_x[valid_pts]]
      self._mask_vol_cpu[valid_vox_x, valid_vox_y, valid_vox_z] = old_mask | new_mask

  def get_volume(self):
    if self.gpu_mode:
      cuda.memcpy_dtoh(self._tsdf_vol_cpu, self._tsdf_vol_gpu)
      cuda.memcpy_dtoh(self._occl_vol_cpu, self._occl_vol_gpu)
      cuda.memcpy_dtoh(self._color_vol_cpu, self._color_vol_gpu)
      cuda.memcpy_dtoh(self._mask_vol_cpu, self._mask_vol_gpu)
    return self._tsdf_vol_cpu, self._occl_vol_cpu, self._color_vol_cpu, self._mask_vol_cpu

  def get_point_cloud(self):
    """Extract a point cloud from the voxel volume.
    """
    tsdf_vol, occl_vol, color_vol, mask_vol = self.get_volume()

    # Marching cubes
    verts = measure.marching_cubes(tsdf_vol, mask=(tsdf_vol > -0.5) & (tsdf_vol < 0.9), level=0)[0]
    verts_ind = np.round(verts).astype(int)
    verts = verts*self._voxel_size + self._vol_origin

    # Get vertex colors
    rgb_vals = color_vol[verts_ind[:, 0], verts_ind[:, 1], verts_ind[:, 2]]
    colors_b = np.floor(rgb_vals / self._color_const)
    colors_g = np.floor((rgb_vals - colors_b*self._color_const) / 256)
    colors_r = rgb_vals - colors_b*self._color_const - colors_g*256
    colors = np.floor(np.asarray([colors_r, colors_g, colors_b])).T
    colors = colors.astype(np.uint8)

    # Get mask
    mask = mask_vol[verts_ind[:, 0], verts_ind[:, 1], verts_ind[:, 2]].reshape((-1, 1))

    pc = np.hstack([verts, colors, mask])
    return pc

  def get_downsampled_all_voxels_pcd_and_voxel_mask(self, reduce=10):
      """
      To find which voxels the transition overlaps, they want this grid of where the points on the downsampled point cloud are
      So they downsample the point cloud, create a grid from the sizes, and creates points in a certain order

      For the real query of the point cloud, the input is the tsdf volume raveled.

      Possible issues:
      {
      The position offset of the point cloud is off
      The downsampled voxel distance between points is off
      } Print both clouds

      {
      The order of the points is off
      } See when indexing the point cloud about the real point cloud shows a matching version
      """

      # Get the voxel grid data
      tsdf_vol, occl_vol, color_vol, mask_vol = self.get_volume()

      # Downsample everything using strided slicing
      #(occl_vol > -100) & (occl_vol < 0)
      occupied_mask = ((occl_vol > -100) & (occl_vol < 0)) #Points that are closer to 0 than -0.7 and points that are closer to 0 than 0.99
      occupied_mask = occupied_mask | ((tsdf_vol > -0.5) & (tsdf_vol < 0.9))
      tsdf_vol = tsdf_vol[::reduce, ::reduce, ::reduce]
      color_vol = color_vol[::reduce, ::reduce, ::reduce]
      mask_vol = mask_vol[::reduce, ::reduce, ::reduce]

      # Adjust voxel size for proper scaling
      downsampled_voxel_size = self._voxel_size * reduce 

      # Generate voxel grid coordinates, so this is just an evenly spaced 3d box
      x, y, z = np.meshgrid(
          np.arange(tsdf_vol.shape[0]),
          np.arange(tsdf_vol.shape[1]),
          np.arange(tsdf_vol.shape[2]),
          indexing='ij'  # Ensures correct (x, y, z) order
      )

      # Flatten into lists of coordinates, [xyz, points].T
      points = np.vstack((x.ravel(), y.ravel(), z.ravel())).T
      #Now the grid of points needs to be scaled down to how far voxels are from each other and add the world origin
      points = points * downsampled_voxel_size + self._vol_origin  # Scale to world coordinates

      # Filter only voxels that are free space (mask_vol == 1)
      # valid_mask = mask_vol.ravel() == 1
      # points = points[valid_mask]
      #occupied_mask = ((tsdf_vol > -0.5) & (tsdf_vol < 0.9)).ravel()
      occupied_mask = occupied_mask[::reduce, ::reduce, ::reduce].ravel()

      pc = points

      #occupied_mask = torch.from_numpy(occupied_mask).to(torch.bool)
      return pc, occupied_mask

  def get_downsampled_all_voxels_pcd(self, reduce=10):
    #Returns [points, 3]
    pc, _ = self.get_downsampled_all_voxels_pcd_and_voxel_mask(reduce)
    return pc

  def get_downsampled_voxel_collision_mask(self, reduce=10):
    #Return [points, 1] bool
    _, occupied_mask = self.get_downsampled_all_voxels_pcd_and_voxel_mask(reduce)
    return occupied_mask

  def get_mesh(self):
    """Compute a mesh from the voxel volume using marching cubes.
    """
    tsdf_vol, occl_vol, color_vol, mask_vol = self.get_volume()

    # Marching cubes
    verts, faces, norms, vals = measure.marching_cubes(tsdf_vol, mask=(tsdf_vol > -0.5) & (tsdf_vol < 0.5), level=0)
    verts_ind = np.round(verts).astype(int)
    verts = verts*self._voxel_size+self._vol_origin  # voxel grid coordinates to world coordinates

    # Get vertex colors
    rgb_vals = color_vol[verts_ind[:,0], verts_ind[:,1], verts_ind[:,2]]
    colors_b = np.floor(rgb_vals/self._color_const)
    colors_g = np.floor((rgb_vals-colors_b*self._color_const)/256)
    colors_r = rgb_vals-colors_b*self._color_const-colors_g*256
    colors = np.floor(np.asarray([colors_r,colors_g,colors_b])).T
    colors = colors.astype(np.uint8)
    return verts, faces, norms, colors


def rigid_transform(xyz, transform):
  """Applies a rigid transform to an (N, 3) pointcloud.
  """
  xyz_h = np.hstack([xyz, np.ones((len(xyz), 1), dtype=np.float32)])
  xyz_t_h = np.dot(transform, xyz_h.T).T
  return xyz_t_h[:, :3]


def get_view_frustum(depth_im, cam_intr, cam_pose):
  """Get corners of 3D camera view frustum of depth image
  """
  im_h = depth_im.shape[0]
  im_w = depth_im.shape[1]
  max_depth = np.max(depth_im)
  view_frust_pts = np.array([
    (np.array([0,0,0,im_w,im_w])-cam_intr[0,2])*np.array([0,max_depth,max_depth,max_depth,max_depth])/cam_intr[0,0],
    (np.array([0,0,im_h,0,im_h])-cam_intr[1,2])*np.array([0,max_depth,max_depth,max_depth,max_depth])/cam_intr[1,1],
    np.array([0,max_depth,max_depth,max_depth,max_depth])
  ])
  view_frust_pts = rigid_transform(view_frust_pts.T, cam_pose).T
  return view_frust_pts


def meshwrite(filename, verts, faces, norms, colors):
  """Save a 3D mesh to a polygon .ply file.
  """
  # Write header
  ply_file = open(filename,'w')
  ply_file.write("ply\n")
  ply_file.write("format ascii 1.0\n")
  ply_file.write("element vertex %d\n"%(verts.shape[0]))
  ply_file.write("property float x\n")
  ply_file.write("property float y\n")
  ply_file.write("property float z\n")
  ply_file.write("property float nx\n")
  ply_file.write("property float ny\n")
  ply_file.write("property float nz\n")
  ply_file.write("property uchar red\n")
  ply_file.write("property uchar green\n")
  ply_file.write("property uchar blue\n")
  ply_file.write("element face %d\n"%(faces.shape[0]))
  ply_file.write("property list uchar int vertex_index\n")
  ply_file.write("end_header\n")

  # Write vertex list
  for i in range(verts.shape[0]):
    ply_file.write("%f %f %f %f %f %f %d %d %d\n"%(
      verts[i,0], verts[i,1], verts[i,2],
      norms[i,0], norms[i,1], norms[i,2],
      colors[i,0], colors[i,1], colors[i,2],
    ))

  # Write face list
  for i in range(faces.shape[0]):
    ply_file.write("3 %d %d %d\n"%(faces[i,0], faces[i,1], faces[i,2]))

  ply_file.close()


def pcwrite(filename, xyzrgb):
  """Save a point cloud to a polygon .ply file.
  """
  xyz = xyzrgb[:, :3]
  rgb = xyzrgb[:, 3:6].astype(np.uint8)

  # Write header
  ply_file = open(filename,'w')
  ply_file.write("ply\n")
  ply_file.write("format ascii 1.0\n")
  ply_file.write("element vertex %d\n"%(xyz.shape[0]))
  ply_file.write("property float x\n")
  ply_file.write("property float y\n")
  ply_file.write("property float z\n")
  ply_file.write("property uchar red\n")
  ply_file.write("property uchar green\n")
  ply_file.write("property uchar blue\n")
  ply_file.write("end_header\n")

  # Write vertex list
  for i in range(xyz.shape[0]):
    ply_file.write("%f %f %f %d %d %d\n"%(
      xyz[i, 0], xyz[i, 1], xyz[i, 2],
      rgb[i, 0], rgb[i, 1], rgb[i, 2],
    ))
