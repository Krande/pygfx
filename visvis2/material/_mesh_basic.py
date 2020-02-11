from ._base import Material

import wgpu  # only for flags/enums
import python_shader
from python_shader import vec3, vec4


@python_shader.python2shader
def vertex_shader(
    pos: (python_shader.RES_INPUT, 0, vec3),
    # transform: (python_shader.RES_UNIFORM, 0, mat4),
    out_pos: (python_shader.RES_OUTPUT, "Position", vec4),
):
    # out_pos = pos * transform
    out_pos = pos  # noqa - shader assign to input arg


@python_shader.python2shader
def fragment_shader(out_color: (python_shader.RES_OUTPUT, 0, vec4),):
    out_color = vec4(1.0, 0.0, 0.0, 1.0)  # noqa - shader assign to input arg


class MeshBasicMaterial(Material):
    def __init__(self):
        self.color = (255.0, 0.0, 0.0)

    def get_renderer_info_wgpu(self):
        return {
            "shaders": [vertex_shader, fragment_shader],
            "primitiveTopology": wgpu.PrimitiveTopology.triangle_list,
        }

    def get_renderer_info_svg(self):
        raise NotImplementedError()
