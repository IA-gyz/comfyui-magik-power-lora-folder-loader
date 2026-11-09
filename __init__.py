from typing_extensions import override
from comfy_api.latest import ComfyExtension, io

from .magik_power_lora_folder_loader import MagikPowerLoraFolderLoader

WEB_DIRECTORY = "./js"


class MagikPowerLoraFolderLoaderExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [MagikPowerLoraFolderLoader]


async def comfy_entrypoint() -> MagikPowerLoraFolderLoaderExtension:
    return MagikPowerLoraFolderLoaderExtension()
