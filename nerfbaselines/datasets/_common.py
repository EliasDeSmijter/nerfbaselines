import warnings
import gc
import logging
import os
import struct
import numpy as np
import PIL.Image
import PIL.ExifTags
from tqdm import tqdm
from typing import Optional, TypeVar, Tuple, Union, List, Sequence, Dict, cast, overload
from ..cameras import camera_model_to_int
from ..types import Dataset, Literal, Cameras, UnloadedDataset
from .. import cameras
from ..utils import padded_stack
from ..pose_utils import rotation_matrix, pad_poses, unpad_poses, viewmatrix, apply_transform


TDataset = TypeVar("TDataset", bound=Union[Dataset, UnloadedDataset])


def single(xs):
    out = None
    for x in xs:
        if out is not None:
            raise ValueError("Expected single value, got multiple")
        out = x
    if out is None:
        raise ValueError("Expected single value, got none")
    return out


def get_transform_poses_pca(poses):
    t = poses[:, :3, 3]
    t_mean = t.mean(axis=0)
    t = t - t_mean

    eigval, eigvec = np.linalg.eig(t.T @ t)
    # Sort eigenvectors in order of largest to smallest eigenvalue.
    inds = np.argsort(eigval)[::-1]
    eigvec = eigvec[:, inds]
    rot = eigvec.T
    if np.linalg.det(rot) < 0:
        rot = np.diag(np.array([1, 1, -1])) @ rot

    transform = np.concatenate([rot, rot @ -t_mean[:, None]], -1)
    poses_recentered = unpad_poses(transform @ pad_poses(poses))
    transform = np.concatenate([transform, np.eye(4)[3:]], axis=0)

    # Flip coordinate system if z component of y-axis is positive
    if poses_recentered.mean(axis=0)[2, 1] > 0:
        transform = np.diag(np.array([1, -1, -1, 1])) @ transform

    # Just make sure it's it in the [-1, 1]^3 cube
    scale_factor = 1.0 / np.max(np.abs(poses_recentered[:, :3, 3]))
    transform = np.diag(np.array([scale_factor] * 3 + [1])) @ transform
    return transform


def focus_point_fn(poses, xnp = np):
    """Calculate nearest point to all focal axes in poses."""
    directions, origins = poses[:, :3, 2:3], poses[:, :3, 3:4]
    m = xnp.eye(3) - directions * xnp.transpose(directions, [0, 2, 1])
    mt_m = xnp.transpose(m, [0, 2, 1]) @ m
    focus_pt = xnp.linalg.inv(mt_m.mean(0)) @ (mt_m @ origins).mean(0)[:, 0]
    return focus_pt


def get_default_viewer_transform(poses, dataset_type: Optional[str]) -> Tuple[np.ndarray, np.ndarray]:
    if dataset_type == "object-centric":
        transform = get_transform_poses_pca(poses)

        poses = apply_transform(transform, poses)
        lookat = focus_point_fn(poses)

        poses[:, :3, 3] -= lookat
        transform[:3, 3] -= lookat
        return transform, poses[0]

    elif dataset_type == "forward-facing":
        raise NotImplementedError("Forward-facing dataset type is not supported")
    elif dataset_type is None:
        # Unknown dataset type
        # We move all center the scene on the mean of the camera origins
        # and reorient the scene so that the average camera up is up
        origins = poses[..., :3, 3]
        mean_origin = np.mean(origins, 0)
        translation = mean_origin
        up = np.mean(poses[:, :3, 1], 0)
        up = -up / np.linalg.norm(up)

        rotation = rotation_matrix(up, np.array([0, 0, 1], dtype=up.dtype))
        transform = np.concatenate([rotation, rotation @ -translation[..., None]], -1)
        transform = np.concatenate([transform, np.array([[0, 0, 0, 1]], dtype=transform.dtype)], 0)

        # Scale so that cameras fit in a 2x2x2 cube centered at the origin
        maxlen = np.quantile(np.abs(poses[..., 0:3, 3] - mean_origin[None]).max(-1), 0.95) * 1.1
        dataparser_scale = float(1 / maxlen)
        transform = np.diag([dataparser_scale, dataparser_scale, dataparser_scale, 1]) @ transform

        camera = apply_transform(transform, poses[0])
        return transform, camera
    else:
        raise ValueError(f"Dataset type {dataset_type} is not supported")


def _dataset_undistort_unsupported(dataset: Dataset, supported_camera_models):
    assert dataset["images"] is not None, "Images must be loaded"
    supported_models_int = set(camera_model_to_int(x) for x in supported_camera_models)
    undistort_tasks = []
    for i, camera in enumerate(dataset["cameras"]):
        if camera.camera_types.item() in supported_models_int:
            continue
        undistort_tasks.append((i, camera))
    if len(undistort_tasks) == 0:
        return False

    was_list = isinstance(dataset["images"], list)
    new_images = list(dataset["images"])
    new_sampling_masks = (
        list(dataset["sampling_masks"]) if dataset["sampling_masks"] is not None else None
    )
    dataset["images"] = new_images
    dataset["sampling_masks"] = new_sampling_masks

    # Release memory here
    gc.collect()

    for i, camera in tqdm(undistort_tasks, desc="undistorting images", dynamic_ncols=True):
        undistorted_camera = cameras.undistort_camera(camera)
        ow, oh = camera.image_sizes
        if dataset["file_paths"] is not None:
            dataset["file_paths"][i] = os.path.join(
                "/undistorted", os.path.split(dataset["file_paths"][i])[-1]
            )
        if dataset["sampling_mask_paths"] is not None:
            dataset["sampling_mask_paths"][i] = os.path.join(
                "/undistorted-masks", os.path.split(dataset["sampling_mask_paths"][i])[-1]
            )
        warped = cameras.warp_image_between_cameras(
            camera, undistorted_camera, new_images[i][:oh, :ow]
        )
        new_images[i] = warped
        if new_sampling_masks is not None:
            warped = cameras.warp_image_between_cameras(camera, undistorted_camera, new_sampling_masks[i][:oh, :ow])
            new_sampling_masks[i] = warped
        # IMPORTANT: camera is modified in-place
        dataset["cameras"][i] = undistorted_camera
    if not was_list:
        dataset["images"] = padded_stack(new_images)
        dataset["sampling_masks"] = (
            padded_stack(new_sampling_masks) if new_sampling_masks is not None else None
        )
    dataset["file_paths_root"] = "/undistorted"
    return True


METADATA_COLUMNS = ["exposure"]
DatasetType = Literal["object-centric", "forward-facing"]


def get_scene_scale(cameras: Cameras, dataset_type: Optional[DatasetType]):
    if dataset_type == "object-centric":
        return float(np.percentile(np.linalg.norm(cameras.poses[..., :3, 3] - cameras.poses[..., :3, 3].mean(), axis=-1), 90))

    elif dataset_type == "forward-facing":
        assert cameras.nears_fars is not None, "Forward-facing dataset must set z-near and z-far"
        return float(cameras.nears_fars.mean())

    elif dataset_type is None:
        return float(np.percentile(np.linalg.norm(cameras.poses[..., :3, 3] - cameras.poses[..., :3, 3].mean(), axis=-1), 90))
    
    else:
        raise ValueError(f"Dataset type {dataset_type} is not supported")


def get_image_metadata(image: PIL.Image.Image):
    # Metadata format: [ exposure, ]
    values = {}
    try:
        exif_pil = image.getexif()
    except AttributeError:
        exif_pil = image._getexif()  # type: ignore
    if exif_pil is not None:
        exif = {PIL.ExifTags.TAGS[k]: v for k, v in exif_pil.items() if k in PIL.ExifTags.TAGS}
        if "ExposureTime" in exif and "ISOSpeedRatings" in exif:
            shutters = exif["ExposureTime"]
            isos = exif["ISOSpeedRatings"]
            exposure = shutters * isos / 1000.0
            values["exposure"] = exposure
    return np.array([values.get(c, np.nan) for c in METADATA_COLUMNS], dtype=np.float32)


def dataset_load_features(
    dataset: UnloadedDataset, features=None, supported_camera_models=None
) -> Dataset:
    if features is None:
        features = frozenset(("color",))
    if supported_camera_models is None:
        supported_camera_models = frozenset(("pinhole",))
    images: List[np.ndarray] = []
    image_sizes = []
    all_metadata = []
    resize = dataset["metadata"].get("downscale_loaded_factor")
    if resize == 1:
        resize = None

    i = 0
    for p in tqdm(dataset["file_paths"], desc="loading images", dynamic_ncols=True):
        if str(p).endswith(".bin"):
            assert dataset["metadata"]["color_space"] == "linear"
            with open(p, "rb") as f:
                data_bytes = f.read()
                h, w = struct.unpack("ii", data_bytes[:8])
                image = (
                    np.frombuffer(
                        data_bytes, dtype=np.float16, count=h * w * 4, offset=8
                    )
                    .astype(np.float32)
                    .reshape([h, w, 4])
                )
            metadata = np.array(
                [np.nan for _ in range(len(METADATA_COLUMNS))], dtype=np.float32
            )
        else:
            assert dataset["metadata"]["color_space"] == "srgb"
            pil_image = PIL.Image.open(p)
            metadata = get_image_metadata(pil_image)
            if resize is not None:
                w, h = pil_image.size
                new_size = round(w/resize), round(h/resize)
                pil_image = pil_image.resize(new_size, PIL.Image.BICUBIC)
                warnings.warn(f"Resized image with a factor of {resize}")

            image = np.array(pil_image, dtype=np.uint8)
        images.append(image)
        image_sizes.append([image.shape[1], image.shape[0]])
        all_metadata.append(metadata)
        i += 1

    logging.debug(f"Loaded {len(images)} images")

    if dataset["sampling_mask_paths"] is not None:
        sampling_masks = []
        for p in tqdm(dataset["sampling_mask_paths"], desc="loading sampling masks", dynamic_ncols=True):
            sampling_mask = PIL.Image.open(p).convert("L")
            if resize is not None:
                w, h = sampling_mask.size
                new_size = round(w*resize), round(h*resize)
                sampling_mask = sampling_mask.resize(new_size, PIL.Image.NEAREST)
                warnings.warn(f"Resized sampling mask with a factor of {resize}")

            sampling_masks.append(np.array(sampling_mask, dtype=np.uint8).astype(bool))
        dataset["sampling_masks"] = sampling_masks  # padded_stack(sampling_masks)
        logging.debug(f"Loaded {len(sampling_masks)} sampling masks")

    if resize is not None:
        # Replace all paths with the resized paths
        dataset["file_paths"] = [
            os.path.join("/resized", os.path.relpath(p, dataset["file_paths_root"])) 
            for p in dataset["file_paths"]]
        dataset["file_paths_root"] = "/resized"
        if dataset["sampling_mask_paths"] is not None:
            dataset["sampling_mask_paths"] = [
                os.path.join("/resized-sampling-masks", os.path.relpath(p, dataset["sampling_mask_paths_root"])) 
                for p in dataset["sampling_mask_paths"]]
            dataset["sampling_mask_paths_root"] = "/resized-sampling-masks"

    dataset["images"] = images  # padded_stack(images)

    # Replace image sizes and metadata
    cameras = dataset["cameras"]
    image_sizes = np.array(image_sizes, dtype=np.int32)
    multipliers = image_sizes.astype(cameras.intrinsics.dtype) / cameras.image_sizes.astype(cameras.intrinsics.dtype)
    multipliers = np.concatenate([multipliers, multipliers], -1)
    dataset["cameras"] = cameras.replace(
        image_sizes=image_sizes, 
        intrinsics=cameras.intrinsics * multipliers, 
        metadata=np.stack(all_metadata, 0))

    print(dataset["cameras"].intrinsics[0])
    print(dataset["cameras"].image_sizes[0])
    print(dataset["cameras"].intrinsics.shape)

    if supported_camera_models is not None:
        if _dataset_undistort_unsupported(cast(Dataset, dataset), supported_camera_models):
            logging.warning(
                "Some cameras models are not supported by the method. Images have been undistorted. Make sure to use the undistorted images for training."
            )
    return cast(Dataset, dataset)


class DatasetNotFoundError(Exception):
    pass


class MultiDatasetError(DatasetNotFoundError):
    def __init__(self, errors, message):
        self.errors = errors
        self.message = message
        super().__init__(message + "\n" + "".join(f"\n  {name}: {error}" for name, error in errors.items()))

    def write_to_logger(self, color=True, terminal_width=None):
        if terminal_width is None:
            terminal_width = 120
            try:
                terminal_width = min(os.get_terminal_size().columns, 120)
            except OSError:
                pass
        message = self.message
        if color:
            message = "\33[0m\33[31m" + message + "\33[0m"
        for name, error in self.errors.items():
            prefix = f"   {name}: "
            mlen = terminal_width - len(prefix)
            prefixlen = len(prefix)
            if color:
                prefix = f"\33[96m{prefix}\33[0m"
            rows = [error[i : i + mlen] for i in range(0, len(error), mlen)]
            mdetail = f'\n{" "*prefixlen}'.join(rows)
            message += f"\n{prefix}{mdetail}"
        logging.error(message)


def dataset_index_select(dataset: TDataset, i: Union[slice, int, list, np.ndarray]) -> TDataset:
    assert isinstance(i, (slice, int, list, np.ndarray))
    dataset_len = len(dataset["file_paths"])

    def index(key, obj):
        if obj is None:
            return None
        if key == "cameras":
            if len(obj) == 1:
                return obj if isinstance(i, int) else obj
            return obj[i]
        if isinstance(obj, np.ndarray):
            if obj.shape[0] == 1:
                return obj[0] if isinstance(i, int) else obj
            obj = obj[i]
            return obj
        if isinstance(obj, list):
            indices = np.arange(dataset_len)[i]
            if indices.ndim == 0:
                return obj[indices]
            return [obj[i] for i in indices]
        raise ValueError(f"Cannot index object of type {type(obj)} at key {key}")

    _dataset = cast(Dict, dataset.copy())
    _dataset.update({k: index(k, v) for k, v in dataset.items() if k not in {
        "file_paths_root", 
        "sampling_mask_paths_root", 
        "points3D_xyz", 
        "points3D_rgb", 
        "metadata"}})
    return cast(TDataset, _dataset)


@overload
def construct_dataset(*,
                      cameras: Cameras,
                      file_paths: Sequence[str],
                      images: Union[np.ndarray, List[np.ndarray]],
                      sampling_mask_paths: Optional[Sequence[str]] = ...,
                      file_paths_root: Optional[str] = ...,
                      sampling_masks: Optional[Union[np.ndarray, List[np.ndarray]]] = ...,  # [N][H, W]
                      points3D_xyz: Optional[np.ndarray] = ...,  # [M, 3]
                      points3D_rgb: Optional[np.ndarray] = ...,  # [M, 3]
                      metadata: Dict) -> Dataset:
    ...


@overload
def construct_dataset(*,
                      cameras: Cameras,
                      file_paths: Sequence[str],
                      images: Literal[None] = None,
                      sampling_mask_paths: Optional[Sequence[str]] = ...,
                      file_paths_root: Optional[str] = ...,
                      sampling_masks: Optional[Union[np.ndarray, List[np.ndarray]]] = ...,  # [N][H, W]
                      points3D_xyz: Optional[np.ndarray] = ...,  # [M, 3]
                      points3D_rgb: Optional[np.ndarray] = ...,  # [M, 3]
                      metadata: Dict) -> UnloadedDataset:
    ...


def construct_dataset(*,
                      cameras: Cameras,
                      file_paths: Sequence[str],
                      file_paths_root: Optional[str] = None,
                      images: Optional[Union[np.ndarray, List[np.ndarray]]] = None,  # [N][H, W, 3]
                      sampling_mask_paths: Optional[Sequence[str]] = None,
                      sampling_mask_paths_root: Optional[str] = None,
                      sampling_masks: Optional[Union[np.ndarray, List[np.ndarray]]] = None,  # [N][H, W]
                      points3D_xyz: Optional[np.ndarray] = None,  # [M, 3]
                      points3D_rgb: Optional[np.ndarray] = None,  # [M, 3]
                      metadata: Dict) -> Union[UnloadedDataset, Dataset]:
    if file_paths_root is None:
        file_paths_root = os.path.commonpath(file_paths)
    if sampling_mask_paths_root is None and sampling_mask_paths is not None:
        sampling_mask_paths_root = os.path.commonpath(sampling_mask_paths)
    if file_paths_root is None:
        file_paths_root = os.path.commonpath(file_paths)
    return UnloadedDataset(
        cameras=cameras,
        file_paths=list(file_paths),
        sampling_mask_paths=list(sampling_mask_paths) if sampling_mask_paths is not None else None,
        sampling_mask_paths_root=sampling_mask_paths_root,
        file_paths_root=file_paths_root,
        images=images,
        sampling_masks=sampling_masks,
        points3D_xyz=points3D_xyz,
        points3D_rgb=points3D_rgb,
        metadata=metadata
    )

