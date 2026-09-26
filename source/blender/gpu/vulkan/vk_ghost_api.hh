/* SPDX-FileCopyrightText: 2022 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup gpu
 */

#pragma once

#include <cstdint>

#include <vulkan/vulkan_core.h>

/** This file contains API that the GHOST_ContextVK can invoke directly. */

namespace blender::gpu {

/**
 * Is the driver of the given physical device supported?
 *
 * There are some drivers that have known issues and should not be used. This check needs to be
 * identical between GPU module and GHOST, otherwise GHOST can still select a device which isn't
 * supported.
 *
 * For example on a Linux machine where LLVMPIPE is installed and an not supported NVIDIA driver
 * Blender would detect a supported configuration using LLVMPIPE, but GHOST could still select the
 * unsupported NVIDIA driver.
 *
 * Returns true when supported, false when not supported.
 */
bool GPU_vulkan_is_supported_driver(VkPhysicalDevice vk_physical_device);

const char *to_string(VkResult result);

/**
 * Minimum Vulkan features a physical device must report for Blender startup.
 *
 * One bit per required feature; 0 means the device meets the minimum. This check needs to be
 * identical between the GPU module (`missing_capabilities_get` in `vk_backend.cc`) and GHOST
 * (`select_physical_device` in `GHOST_ContextVK.cc`), otherwise GHOST can still select a device
 * the backend rejects, or reject a device the backend would accept.
 */
enum class GPUVulkanMinimumFeature : uint32_t {
  GeometryShader = 1u << 0,
  FragmentStoresAndAtomics = 1u << 1,
  DualSrcBlend = 1u << 2,
  ImageCubeArray = 1u << 3,
  MultiDrawIndirect = 1u << 4,
  ShaderDrawParameters = 1u << 5,
};

/**
 * Bitmask of #GPUVulkanMinimumFeature entries the device is missing.
 *
 * `vertexPipelineStoresAndAtomics` and `shaderClipDistance` are intentionally NOT required:
 * vertex-stage storage-buffer reads (what startup draw shaders use) need neither stores nor
 * atomics, and clip distance is only declared by shaders when viewport clipping planes are
 * enabled (the default viewport has none). Requiring them excludes otherwise-capable mobile
 * GPUs: ARM Mali-G720 reports neither, while the Galaxy S24 Ultra test device (Adreno 750)
 * reports both. Same precedent as multiViewport/logicOp, which are already optional because
 * all Adreno lack logicOp.
 */
inline uint32_t GPU_vulkan_missing_minimum_features(
    const VkPhysicalDeviceFeatures &features,
    const VkPhysicalDeviceVulkan11Features &features_11)
{
  uint32_t missing = 0;
#ifndef __APPLE__
  /* Features currently not supported by Mesa KosmicKrisp. */
  if (features.geometryShader == VK_FALSE) {
    missing |= uint32_t(GPUVulkanMinimumFeature::GeometryShader);
  }
#endif
  if (features.fragmentStoresAndAtomics == VK_FALSE) {
    missing |= uint32_t(GPUVulkanMinimumFeature::FragmentStoresAndAtomics);
  }
  if (features.dualSrcBlend == VK_FALSE) {
    missing |= uint32_t(GPUVulkanMinimumFeature::DualSrcBlend);
  }
  if (features.imageCubeArray == VK_FALSE) {
    missing |= uint32_t(GPUVulkanMinimumFeature::ImageCubeArray);
  }
  if (features.multiDrawIndirect == VK_FALSE) {
    missing |= uint32_t(GPUVulkanMinimumFeature::MultiDrawIndirect);
  }
  if (features_11.shaderDrawParameters == VK_FALSE) {
    missing |= uint32_t(GPUVulkanMinimumFeature::ShaderDrawParameters);
  }
  return missing;
}

}  // namespace blender::gpu
