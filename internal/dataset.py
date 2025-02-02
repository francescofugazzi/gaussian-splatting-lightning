import concurrent.futures
import json
import math
import os.path
import shutil
from concurrent.futures import ThreadPoolExecutor
from rich.progress import track
import random
from typing import Literal, Tuple, Optional
from PIL import Image
import numpy as np
import torch.utils.data
from lightning import LightningDataModule
from lightning.pytorch.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS

from internal.cameras.cameras import Camera
from internal.dataparsers import ImageSet
from internal.configs.dataset import DatasetParams
from internal.dataparsers.colmap_dataparser import ColmapDataParser
from internal.dataparsers.blender_dataparser import BlenderDataParser
from internal.dataparsers.nsvf_dataparser import NSVFDataParser
from internal.utils.graphics_utils import store_ply, BasicPointCloud

from tqdm import tqdm


class Dataset(torch.utils.data.Dataset):
    def __init__(self, image_set: ImageSet) -> None:
        super().__init__()
        self.image_set = image_set

    def __len__(self):
        return len(self.image_set)

    def get_image(self, index) -> Tuple[str, torch.Tensor, Optional[torch.Tensor]]:
        # TODO: resize

        pil_image = Image.open(self.image_set.image_paths[index])
        image = torch.from_numpy(np.array(pil_image, dtype="uint8").astype("float32") / 255.0)
        # remove alpha channel
        if image.shape[2] == 4:
            # TODO: sync background color with model.background_color
            image = image[..., :3]

        mask = None
        if self.image_set.mask_paths[index] is not None:
            pil_image = Image.open(self.image_set.mask_paths[index])
            mask = torch.from_numpy(np.array(pil_image))
            # mask must be single channel
            assert len(mask.shape) == 2, "the mask image must be single channel"
            # the shape of the mask must match to the image
            assert mask.shape[:2] == image.shape[:2], \
                "the shape of mask {} doesn't match to the image {}".format(mask.shape[:2], image.shape[:2])
            mask = (mask == 0).unsqueeze(-1).expand(*image.shape)  # True is the masked pixels
            mask = mask.permute(2, 0, 1)  # [channel, height, width]

        image = image.permute(2, 0, 1)  # [channel, height, width]

        return self.image_set.image_names[index], image, mask

    def __getitem__(self, index) -> Tuple[Camera, Tuple]:
        return self.image_set.cameras[index], self.get_image(index)


class CacheDataLoader(torch.utils.data.DataLoader):
    def __init__(
            self,
            dataset: torch.utils.data.Dataset,
            max_cache_num: int,
            shuffle: bool,
            seed: int = -1,
            distributed: bool = False,
            world_size: int = -1,
            global_rank: int = -1,
            **kwargs,
    ):
        assert kwargs.get("batch_size", 1) == 1, "only batch_size=1 is supported"

        self.dataset = dataset

        super().__init__(dataset=dataset, **kwargs)

        self.shuffle = shuffle
        self.max_cache_num = max_cache_num

        # image indices to use
        self.indices = list(range(len(self.dataset)))
        if distributed is True and self.max_cache_num != 0:
            assert world_size > 0
            assert global_rank >= 0
            image_num_to_use = math.ceil(len(self.indices) / world_size)
            start = global_rank * image_num_to_use
            end = start + image_num_to_use
            indices = self.indices[start:end]
            indices += self.indices[:image_num_to_use - len(indices)]
            self.indices = indices

            print("#{} distributed indices (total: {}): {}".format(os.getpid(), len(self.indices), self.indices))

        # cache all images if max_cache_num > len(dataset)
        if self.max_cache_num >= len(self.indices):
            self.max_cache_num = -1

        self.num_workers = kwargs.get("num_workers", 0)

        if self.max_cache_num < 0:
            # cache all data
            print("cache all images")
            self.cached = self._cache_data(self.indices)

        # use dedicated random number generator foreach dataloader
        if self.shuffle is True:
            assert seed >= 0, "seed must be provided when shuffle=True"
            self.generator = torch.Generator()
            self.generator.manual_seed(seed)
            print("#{} dataloader seed to {}".format(os.getpid(), seed))

    def _cache_data(self, indices: list):
        # TODO: speedup image loading
        cached = []
        if self.num_workers > 0:
            with ThreadPoolExecutor(max_workers=self.num_workers) as e:
                for i in tqdm(
                        e.map(self.dataset.__getitem__, indices),
                        total=len(indices),
                        desc="#{} caching images (1st: {})".format(os.getpid(), indices[0]),
                ):
                    cached.append(i)
        else:
            for i in tqdm(indices, desc="#{} loading images (1st: {})".format(os.getpid(), indices[0])):
                cached.append(self.dataset.__getitem__(i))

        return cached

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, idx):
        return self.dataset.__getitem__(idx)

    def __iter__(self):
        # TODO: support batching
        if self.max_cache_num < 0:
            if self.shuffle is True:
                indices = torch.randperm(len(self.cached), generator=self.generator).tolist()  # shuffle for each epoch
                # print("#{} 1st index: {}".format(os.getpid(), indices[0]))
            else:
                indices = list(range(len(self.cached)))

            for i in indices:
                yield self.cached[i]
        else:
            if self.shuffle is True:
                indices = torch.randperm(len(self.indices), generator=self.generator).tolist()  # shuffle for each epoch
                # print("#{} 1st index: {}".format(os.getpid(), indices[0]))
            else:
                indices = self.indices.copy()

            # print("#{} self.max_cache_num={}, indices: {}".format(os.getpid(), self.max_cache_num, indices))

            if self.max_cache_num == 0:
                # no cache
                for i in indices:
                    yield self.__getitem__(i)
            else:
                # cache
                # the list contains the data have not been cached
                not_cached = indices.copy()

                while not_cached:
                    # select self.max_cache_num images
                    to_cache = not_cached[:self.max_cache_num]
                    del not_cached[:self.max_cache_num]

                    # cache
                    cached = self._cache_data(to_cache)

                    for i in cached:
                        yield i


class DataModule(LightningDataModule):
    def __init__(
            self,
            path: str,
            params: DatasetParams,
            type: Literal["colmap", "blender", "nsvf"] = None,
            distributed: bool = False,
    ) -> None:
        r"""Load dataset

            Args:
                path: the path to the dataset

                type: the dataset type
        """

        super().__init__()
        self.save_hyperparameters()

    def setup(self, stage: str) -> None:
        super().setup(stage)

        output_path = self.trainer.lightning_module.hparams["output_path"]

        # store global rank, will be used as the seed of the CacheDataLoader
        self.global_rank = self.trainer.global_rank

        # detect dataset type
        if self.hparams["type"] is None:
            if os.path.isdir(os.path.join(self.hparams["path"], "sparse")) is True:
                self.hparams["type"] = "colmap"
            elif os.path.exists(os.path.join(self.hparams["path"], "transforms_train.json")):
                self.hparams["type"] = "blender"
            else:
                self.hparams["type"] = "nsvf"

        # build dataparser params
        dataparser_params = {
            "path": self.hparams["path"],
            "output_path": output_path,
            "global_rank": self.global_rank,
        }

        if self.hparams["type"] == "colmap":
            dataparser = ColmapDataParser(params=self.hparams["params"].colmap, **dataparser_params)
        elif self.hparams["type"] == "blender":
            dataparser = BlenderDataParser(params=self.hparams["params"].blender, **dataparser_params)
        elif self.hparams["type"] == "nsvf":
            dataparser = NSVFDataParser(params=self.hparams["params"].nsvf, **dataparser_params)
        else:
            raise ValueError("unsupported dataset type {}".format(self.hparams["type"]))

        # load dataset
        self.dataparser_outputs = dataparser.get_outputs()

        self.prune_extent = self.dataparser_outputs.camera_extent
        # add background sphere: https://github.com/graphdeco-inria/gaussian-splatting/issues/300#issuecomment-1756073909
        if self.hparams["params"].add_background_sphere is True:
            # find the scene center and size
            point_max_coordinate = np.max(self.dataparser_outputs.point_cloud.xyz, axis=0)
            point_min_coordinate = np.min(self.dataparser_outputs.point_cloud.xyz, axis=0)
            scene_center = (point_max_coordinate + point_min_coordinate) / 2
            scene_size = np.max(point_max_coordinate - point_min_coordinate)
            scene_radius = scene_size / 2.
            # build unit sphere points
            n_points = self.hparams["params"].background_sphere_points
            samples = np.arange(n_points)
            y = 1 - (samples / float(n_points - 1)) * 2  # y goes from 1 to -1
            radius = np.sqrt(1 - y * y)  # radius at y
            phi = math.pi * (math.sqrt(5.) - 1.)  # golden angle in radians
            theta = phi * samples  # golden angle increment
            x = np.cos(theta) * radius
            z = np.sin(theta) * radius
            unit_sphere_points = np.concatenate([x[:, None], y[:, None], z[:, None]], axis=1)
            # build background sphere
            background_sphere_point_xyz = (unit_sphere_points * scene_radius * self.hparams["params"].background_sphere_distance) + scene_center
            background_sphere_point_rgb = np.asarray(np.random.random(background_sphere_point_xyz.shape) * 255, dtype=np.uint8)
            # add background sphere to scene
            self.dataparser_outputs.point_cloud.xyz = np.concatenate([self.dataparser_outputs.point_cloud.xyz, background_sphere_point_xyz], axis=0)
            self.dataparser_outputs.point_cloud.rgb = np.concatenate([self.dataparser_outputs.point_cloud.rgb, background_sphere_point_rgb], axis=0)
            # increase prune extent
            # TODO: resize scene_extent without changing lr
            self.prune_extent = scene_radius * self.hparams["params"].background_sphere_distance * 1.0001

            print("added {} background sphere points, rescale prune extent from {} to {}".format(n_points, self.dataparser_outputs.camera_extent, self.prune_extent))

        # convert point cloud
        self.point_cloud = BasicPointCloud(
            points=self.dataparser_outputs.point_cloud.xyz,
            colors=self.dataparser_outputs.point_cloud.rgb / 255.,
            normals=np.zeros_like(self.dataparser_outputs.point_cloud.xyz),
        )

        # write some files that SIBR_viewer required
        if self.global_rank == 0 and stage == "fit":
            # write appearance group id
            if self.dataparser_outputs.appearance_group_ids is not None:
                torch.save(
                    self.dataparser_outputs.appearance_group_ids,
                    os.path.join(output_path, "appearance_group_ids.pth"),
                )
                with open(os.path.join(output_path, "appearance_group_ids.json"), "w") as f:
                    json.dump(self.dataparser_outputs.appearance_group_ids, f, indent=4, ensure_ascii=False)

            # write cameras.json
            camera_to_world = torch.linalg.inv(
                torch.transpose(self.dataparser_outputs.train_set.cameras.world_to_camera, 1, 2)
            ).numpy()
            cameras = []
            for idx, image in enumerate(self.dataparser_outputs.train_set):
                image_name, _, _, camera = image
                cameras.append({
                    'id': idx,
                    'img_name': image_name,
                    'width': int(camera.width),
                    'height': int(camera.height),
                    'position': camera_to_world[idx, :3, 3].tolist(),
                    'rotation': [x.tolist() for x in camera_to_world[idx, :3, :3]],
                    'fy': float(camera.fy),
                    'fx': float(camera.fx),
                })
            with open(os.path.join(output_path, "cameras.json"), "w") as f:
                json.dump(cameras, f, indent=4, ensure_ascii=False)

            # save input point cloud to ply file
            store_ply(
                os.path.join(output_path, "input.ply"),
                xyz=self.dataparser_outputs.point_cloud.xyz,
                rgb=self.dataparser_outputs.point_cloud.rgb,
            )

            # write cfg_args
            with open(os.path.join(output_path, "cfg_args"), "w") as f:
                f.write("Namespace(sh_degree={}, white_background={}, source_path='{}')".format(
                    self.trainer.lightning_module.hparams["gaussian"].sh_degree,
                    True if torch.all(self.trainer.lightning_module.background_color == 1.) else False,
                    self.hparams["path"],
                ))

    def train_dataloader(self) -> TRAIN_DATALOADERS:
        return CacheDataLoader(
            Dataset(self.dataparser_outputs.train_set),
            max_cache_num=self.hparams["params"].train_max_num_images_to_cache,
            shuffle=True,
            seed=torch.initial_seed() + self.global_rank,  # seed with global rank
            num_workers=self.hparams["params"].num_workers,
            distributed=self.hparams["distributed"],
            world_size=self.trainer.world_size,
            global_rank=self.trainer.global_rank,
        )

    def test_dataloader(self) -> EVAL_DATALOADERS:
        return CacheDataLoader(
            Dataset(self.dataparser_outputs.val_set),
            max_cache_num=self.hparams["params"].test_max_num_images_to_cache,
            shuffle=False,
            num_workers=0,
        )

    def val_dataloader(self) -> EVAL_DATALOADERS:
        return CacheDataLoader(
            Dataset(self.dataparser_outputs.val_set),
            max_cache_num=self.hparams["params"].val_max_num_images_to_cache,
            shuffle=False,
            num_workers=0,
        )
