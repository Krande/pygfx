"""
Functions to update resources.
"""

import wgpu

from ._utils import to_texture_format, GfxSampler, GfxTextureView
from ._mipmapsutil import get_mip_level_count, generate_texture_mipmaps
from ...resources import Texture, Buffer


# Alternative texture formats that we support by padding channels as needed.
# Maps virtual_format -> (wgpu_format, pad_value, nbytes)
ALTTEXFORMAT = {
    "rgb8snorm": ("rgba8snorm", 127, 1),
    "rgb8unorm": ("rgba8unorm", 255, 1),
    "rgb8sint": ("rgba8sint", 127, 1),
    "rgb8uint": ("rgba8uint", 255, 1),
    "rgb16sint": ("rgba16sint", 32767, 2),
    "rgb16uint": ("rgba16uint", 65535, 2),
    "rgb32sint": ("rgba32sint", 2147483647, 4),
    "rgb32uint": ("rgba32uint", 4294967295, 4),
    "rgb16float": ("rgba16float", 1, 2),
    "rgb32float": ("rgba32float", 1, 4),
}


def update_resource(device, resource, kind):
    """Update the contents of a buffer or texture."""
    if kind == "buffer":
        return update_buffer(device, resource)
    elif kind == "texture":
        return update_texture(device, resource)
    else:
        raise ValueError(f"Invalid resource kind: {kind}")


def update_buffer(device, buffer):
    # Get and reset pending uploads
    pending_uploads = buffer._gfx_pending_uploads
    buffer._gfx_pending_uploads = []
    if pending_uploads:
        buffer._wgpu_usage |= wgpu.BufferUsage.COPY_DST

    wgpu_buffer = ensure_wgpu_object(device, buffer)

    if not pending_uploads:
        return

    # Prepare for data uploads
    bytes_per_item = buffer.nbytes // buffer.nitems

    # Upload any pending data
    for offset, size in pending_uploads:
        subdata = buffer._get_subdata(offset, size)
        # A: map the buffer, writes to it, then unmaps. But we don't offer a mapping API in wgpu-py
        # B: roll data in new buffer, copy from there to existing buffer
        # tmp_buffer = device.create_buffer_with_data(
        #     data=subdata,
        #     usage=wgpu.BufferUsage.COPY_SRC,
        # )
        # boffset, bsize = bytes_per_item * offset, bytes_per_item * size
        # encoder.copy_buffer_to_buffer(tmp_buffer, 0, buffer, boffset, bsize)
        # C: using queue. This may be sugar for B, but it may also be optimized.
        device.queue.write_buffer(
            wgpu_buffer, bytes_per_item * offset, subdata, 0, subdata.nbytes
        )
        # D: A staging buffer/belt https://github.com/gfx-rs/wgpu-rs/blob/master/src/util/belt.rs


def update_texture(device, texture):
    # Get and reset pending uploads
    pending_uploads = texture._gfx_pending_uploads
    texture._gfx_pending_uploads = []
    if pending_uploads:
        texture._wgpu_usage |= wgpu.TextureUsage.COPY_DST

    wgpu_texture = ensure_wgpu_object(device, texture)

    if not pending_uploads:
        return

    # Prepare for data uploads
    fmt = to_texture_format(texture.format)
    pixel_padding = None
    extra_bytes = 0
    if fmt in ALTTEXFORMAT:
        _, pixel_padding, extra_bytes = ALTTEXFORMAT[fmt]
    bytes_per_pixel = texture.nbytes // (
        texture.size[0] * texture.size[1] * texture.size[2]
    )
    if pixel_padding is not None:
        bytes_per_pixel += extra_bytes

    # Upload any pending data
    for offset, size in pending_uploads:
        subdata = texture._get_subdata(offset, size, pixel_padding)
        # B: using a temp buffer
        # tmp_buffer = device.create_buffer_with_data(data=subdata,
        #     usage=wgpu.BufferUsage.COPY_SRC,
        # )
        # encoder.copy_buffer_to_texture(
        #     {
        #         "buffer": tmp_buffer,
        #         "offset": 0,
        #         "bytes_per_row": size[0] * bytes_per_pixel,  # multiple of 256
        #         "rows_per_image": size[1],
        #     },
        #     {
        #         "texture": texture,
        #         "mip_level": 0,
        #         "origin": offset,
        #     },
        #     copy_size=size,
        # )
        # C: using the queue, which may be doing B, but may also be optimized,
        #    and the bytes_per_row limitation does not apply here
        device.queue.write_texture(
            {"texture": wgpu_texture, "origin": offset, "mip_level": 0},
            subdata,
            {"bytes_per_row": size[0] * bytes_per_pixel, "rows_per_image": size[1]},
            size,
        )

    if texture.generate_mipmaps:
        generate_texture_mipmaps(device, texture)


def ensure_wgpu_object(device, resource):
    """Make sure that the resource (buffer texture, sampler, textureView)
    has a wgpu object attached to it. Returns the native wgpu object.
    """
    if resource._wgpu_object is not None:
        return resource._wgpu_object

    if isinstance(resource, Buffer):
        resource._wgpu_object = device.create_buffer(
            size=resource.nbytes, usage=resource._wgpu_usage
        )

    elif isinstance(resource, Texture):
        fmt = to_texture_format(resource.format)
        if fmt in ALTTEXFORMAT:
            fmt = ALTTEXFORMAT[fmt][0]
        usage = resource._wgpu_usage
        if resource.generate_mipmaps:
            usage |= wgpu.TextureUsage.STORAGE_BINDING  # mipmap needs this
            resource._wgpu_mip_level_count = get_mip_level_count(resource)
        resource._wgpu_object = device.create_texture(
            size=resource.size,
            usage=usage,
            dimension=f"{resource.dim}d",
            format=fmt,
            mip_level_count=resource._wgpu_mip_level_count,
            sample_count=1,  # could allow more to implement msaa
        )

    elif isinstance(resource, GfxTextureView):
        wgpu_texture = ensure_wgpu_object(device, resource.texture)
        if resource.is_default_view:
            resource._wgpu_object = wgpu_texture.create_view()
        else:
            fmt = to_texture_format(resource.format)
            fmt = ALTTEXFORMAT.get(fmt, [fmt])[0]
            resource._wgpu_object = wgpu_texture.create_view(
                format=fmt,
                dimension=resource.view_dim,
                aspect=resource.aspect,
                base_mip_level=0,
                mip_level_count=resource.texture._wgpu_mip_level_count,
                base_array_layer=resource.layer_range[0],
                array_layer_count=(resource.layer_range[1] - resource.layer_range[0]),
            )

    elif isinstance(resource, GfxSampler):
        amodes = resource.address_mode.replace(",", " ").split() or ["clamp"]
        while len(amodes) < 3:
            amodes.append(amodes[-1])
        filters = resource.filter.replace(",", " ").split() or ["nearest"]
        while len(filters) < 3:
            filters.append(filters[-1])
        ammap = {"clamp": "clamp-to-edge", "mirror": "mirror-repeat"}
        resource._wgpu_object = device.create_sampler(
            address_mode_u=ammap.get(amodes[0], amodes[0]),
            address_mode_v=ammap.get(amodes[1], amodes[1]),
            address_mode_w=ammap.get(amodes[2], amodes[2]),
            mag_filter=filters[0],
            min_filter=filters[1],
            mipmap_filter=filters[2],
            # lod_min_clamp -> use default 0
            # lod_max_clamp -> use default inf
            # compare -> only not-None for comparison samplers!
        )

    return resource._wgpu_object
