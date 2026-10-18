/*
 * Copyright 2016 The Chromium OS Authors
 * Copyright 2020 The Chromium OS Authors
 * Copyright 2026 ImL1s
 *
 * Use of this source code is governed by a BSD-style license that can be
 * found in tools/minigbm/NOTICES.txt.
 *
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Linear amdgpu backend for minigbm.
 *
 * Upstream amdgpu.c (chromiumos platform/minigbm a2d42f09) registers
 * backend_amdgpu only when DRV_AMDGPU is set, and amdgpu_init() returns
 * -ENODEV unless dlopen() of radeonsi_dri.so succeeds. The amd-hx370 image
 * ships Mesa RADV as vulkan.radeon.so and does not ship radeonsi_dri.so, so
 * that init cannot succeed.
 *
 * This backend keeps the DRM version name "amdgpu" (the string drv_get_backend
 * matches) but allocates linear GTT BOs with DRM_AMDGPU_GEM_CREATE. That ioctl
 * is the kernel uapi, so this file does not link libdrm_amdgpu and does not
 * load a DRI driver. Scanout consumers import the dma-buf. GFX9+ pitch is
 * aligned to 256 bytes, which GFX11.5 (Radeon 890M) requires.
 *
 * If GEM_CREATE fails, allocation falls back to a DRM dumb buffer. Dumb
 * objects are destroyed with DESTROY_DUMB; GEM objects are GEM_CLOSE'd.
 * bo->priv is NULL for GEM and non-NULL for the dumb fallback.
 */

#ifdef DRV_AMDGPU

#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include <amdgpu_drm.h>
#include <drm_fourcc.h>
#include <xf86drm.h>

#include "drv_helpers.h"
#include "drv_priv.h"
#include "util.h"

const char minigbm_amdgpu_build_id[] = "minigbm-amdgpu-linear/no-radeonsi";

static const uint32_t scanout_render_formats[] = { DRM_FORMAT_ARGB8888, DRM_FORMAT_XRGB8888,
						   DRM_FORMAT_ABGR8888, DRM_FORMAT_XBGR8888,
						   DRM_FORMAT_BGR888, DRM_FORMAT_RGB565 };

static const uint32_t texture_only_formats[] = { DRM_FORMAT_R8, DRM_FORMAT_NV12, DRM_FORMAT_NV21,
						 DRM_FORMAT_YVU420, DRM_FORMAT_YVU420_ANDROID };

static int amdgpu_linear_init(struct driver *drv)
{
	drv_add_combinations(drv, scanout_render_formats, ARRAY_SIZE(scanout_render_formats),
			     &LINEAR_METADATA, BO_USE_RENDER_MASK | BO_USE_SCANOUT);

	drv_add_combinations(drv, texture_only_formats, ARRAY_SIZE(texture_only_formats),
			     &LINEAR_METADATA, BO_USE_TEXTURE_MASK);

	drv_modify_combination(drv, DRM_FORMAT_R8, &LINEAR_METADATA,
			       BO_USE_HW_VIDEO_ENCODER | BO_USE_HW_VIDEO_DECODER | BO_USE_CAMERA_READ |
				   BO_USE_CAMERA_WRITE | BO_USE_GPU_DATA_BUFFER |
				   BO_USE_SENSOR_DIRECT_DATA);
	drv_modify_combination(drv, DRM_FORMAT_NV12, &LINEAR_METADATA,
			       BO_USE_HW_VIDEO_ENCODER | BO_USE_HW_VIDEO_DECODER | BO_USE_CAMERA_READ |
				   BO_USE_CAMERA_WRITE);
	drv_modify_combination(drv, DRM_FORMAT_NV21, &LINEAR_METADATA, BO_USE_HW_VIDEO_ENCODER);

	return drv_modify_linear_combinations(drv);
}

/* yv12-helper-begin */
static uint32_t amdgpu_linear_yv12_luma(uint32_t stride, uint32_t stride_align)
{
	if (stride_align > 16)
		stride = (stride + (stride_align * 2) - 1) & ~((stride_align * 2) - 1);
	return stride;
}
/* yv12-helper-end */

static int amdgpu_linear_create(struct bo *bo, uint32_t width, uint32_t height, uint32_t format,
				uint64_t use_flags)
{
	uint32_t stride;
	uint32_t stride_align = 1;
	int ret;
	union drm_amdgpu_gem_create gem_create = { { 0 } };

	if (!drv_get_combination(bo->drv, format, use_flags))
		return -EINVAL;

	stride = drv_stride_from_format(format, width, 0);
	if (use_flags & (BO_USE_HW_MASK | BO_USE_LINEAR)) {
		stride_align = 256;
		stride = ALIGN(stride, stride_align);
	}

	/*
	 * Android YV12 (system/graphics.h) requires chroma stride
	 * ALIGN(luma_stride / 2, 16). drv_bo_from_format aligns every plane
	 * to stride_align and asserts that equality for YVU420_ANDROID.
	 * A 256-byte luma alignment makes chroma ALIGN(luma/2, 256): 640x480
	 * becomes 768/512/512 instead of chroma 384. Rounding luma up to
	 * 2 * stride_align makes luma/2 a multiple of stride_align, so chroma
	 * stays luma/2 and matches the Android rule. YV12 is texture-only here.
	 * Upstream amdgpu.c on GFX10+ (family >= AMDGPU_FAMILY_NV, BO_USE_GPU_HW,
	 * and an odd stride/256) does not double: it uses DRM_FORMAT_YVU420 and
	 * logs. This file always doubles, so a 640-wide texture is luma 1024,
	 * not 768. Radeon 890M is GFX11.5. Sampling that larger LINEAR pitch in
	 * RADV is a hardware gate in the native-entry checklist
	 * (docs/NEW-DEVICE.md), not a QEMU check.
	 */
	if (format == DRM_FORMAT_YVU420_ANDROID)
		stride = amdgpu_linear_yv12_luma(stride, stride_align);

	if (use_flags & (BO_USE_HW_VIDEO_DECODER | BO_USE_HW_VIDEO_ENCODER) &&
	    format != DRM_FORMAT_YVU420_ANDROID)
		height = ALIGN(height, 16);

	ret = drv_bo_from_format(bo, stride, stride_align, height, format);
	if (ret)
		return ret;

	gem_create.in.bo_size = ALIGN(bo->meta.total_size, (uint64_t)PAGE_SIZE);
	gem_create.in.alignment = 256;
	gem_create.in.domains = AMDGPU_GEM_DOMAIN_GTT;
	gem_create.in.domain_flags = 0;

	if (use_flags & (BO_USE_LINEAR | BO_USE_SW_MASK | BO_USE_SCANOUT))
		gem_create.in.domain_flags |= AMDGPU_GEM_CREATE_CPU_ACCESS_REQUIRED;

	/* Same choice as upstream amdgpu_create_bo_linear: USWC for scanout. */
	if ((use_flags & BO_USE_SCANOUT) || !(use_flags & BO_USE_SW_READ_OFTEN))
		gem_create.in.domain_flags |= AMDGPU_GEM_CREATE_CPU_GTT_USWC;

	if (use_flags & BO_USE_PROTECTED)
		gem_create.in.domain_flags |= AMDGPU_GEM_CREATE_ENCRYPTED;

	ret = drmCommandWriteRead(drv_get_fd(bo->drv), DRM_AMDGPU_GEM_CREATE, &gem_create,
				  sizeof(gem_create));
	if (ret < 0) {
		drv_loge("AMDGPU_GEM_CREATE failed (%d); trying dumb buffer\n", ret);
		ret = drv_dumb_bo_create(bo, width, height, format, use_flags);
		if (ret)
			return ret;
		bo->priv = (void *)1;
		bo->meta.format_modifier = DRM_FORMAT_MOD_LINEAR;
		return 0;
	}

	bo->priv = NULL;
	bo->handle.u32 = gem_create.out.handle;
	bo->meta.format_modifier = DRM_FORMAT_MOD_LINEAR;
	return 0;
}

static int amdgpu_linear_create_with_modifiers(struct bo *bo, uint32_t width, uint32_t height,
					       uint32_t format, const uint64_t *modifiers,
					       uint32_t count)
{
	uint32_t i;

	for (i = 0; i < count; i++) {
		if (modifiers[i] == DRM_FORMAT_MOD_LINEAR || modifiers[i] == DRM_FORMAT_MOD_INVALID)
			return amdgpu_linear_create(bo, width, height, format,
						    BO_USE_SCANOUT | BO_USE_LINEAR);
	}

	return -EINVAL;
}

static int amdgpu_linear_destroy(struct bo *bo)
{
	if (bo->priv)
		return drv_dumb_bo_destroy(bo);
	return drv_gem_bo_destroy(bo);
}

static void *amdgpu_linear_map(struct bo *bo, struct vma *vma, uint32_t map_flags)
{
	union drm_amdgpu_gem_mmap gem_map = { { 0 } };
	void *addr;
	int ret;

	if (bo->priv)
		return drv_dumb_bo_map(bo, vma, map_flags);

	gem_map.in.handle = bo->handle.u32;
	ret = drmIoctl(bo->drv->fd, DRM_IOCTL_AMDGPU_GEM_MMAP, &gem_map);
	if (ret) {
		drv_loge("DRM_IOCTL_AMDGPU_GEM_MMAP failed\n");
		return MAP_FAILED;
	}

	vma->length = bo->meta.total_size;
	addr = mmap(0, vma->length, drv_get_prot(map_flags), MAP_SHARED, bo->drv->fd,
		    gem_map.out.addr_ptr);
	return addr;
}

static int amdgpu_linear_import(struct bo *bo, struct drv_import_fd_data *data)
{
	uint64_t modifier = data->format_modifier;

	/*
	 * Linear-only import. DRM_FORMAT_MOD_LINEAR is the layout this
	 * backend allocates. DRM_FORMAT_MOD_INVALID means the caller did not
	 * name a modifier; every combination registered here is linear, so
	 * the implicit layout is linear and is recorded as
	 * DRM_FORMAT_MOD_LINEAR. Any other modifier, including AMD tiled
	 * modifiers, is rejected. There is no DRI fallback.
	 */
	if (modifier != DRM_FORMAT_MOD_LINEAR && modifier != DRM_FORMAT_MOD_INVALID)
		return -EINVAL;

	bo->meta.format_modifier = DRM_FORMAT_MOD_LINEAR;
	return drv_prime_bo_import(bo, data);
}

const struct backend backend_amdgpu = {
	.name = "amdgpu",
	.init = amdgpu_linear_init,
	.bo_create = amdgpu_linear_create,
	.bo_create_with_modifiers = amdgpu_linear_create_with_modifiers,
	.bo_destroy = amdgpu_linear_destroy,
	.bo_import = amdgpu_linear_import,
	.bo_export = drv_prime_bo_export,
	.bo_map = amdgpu_linear_map,
	.bo_unmap = drv_bo_munmap,
	.resolve_format_and_use_flags = drv_resolve_format_and_use_flags_helper,
};

#endif
