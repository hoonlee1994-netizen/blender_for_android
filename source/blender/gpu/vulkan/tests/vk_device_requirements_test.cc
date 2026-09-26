/* SPDX-FileCopyrightText: 2026 Blender Authors
 *
 * SPDX-License-Identifier: Apache-2.0 */

#include "testing/testing.h"

#include "vulkan/vk_ghost_api.hh"

namespace blender::gpu {

/* Synthetic capability sets. Labels describe the fixture only; the helper branches on
 * capability bits, never on vendor or model names. */

static VkPhysicalDeviceFeatures all_minimum_features()
{
  VkPhysicalDeviceFeatures features = {};
  features.geometryShader = VK_TRUE;
  features.vertexPipelineStoresAndAtomics = VK_TRUE;
  features.shaderClipDistance = VK_TRUE;
  features.fragmentStoresAndAtomics = VK_TRUE;
  features.dualSrcBlend = VK_TRUE;
  features.imageCubeArray = VK_TRUE;
  features.multiDrawIndirect = VK_TRUE;
  return features;
}

static VkPhysicalDeviceVulkan11Features all_minimum_features_11()
{
  VkPhysicalDeviceVulkan11Features features_11 = {};
  features_11.shaderDrawParameters = VK_TRUE;
  return features_11;
}

TEST(vulkan_minimum_features, full_feature_device_accepted)
{
  EXPECT_EQ(GPU_vulkan_missing_minimum_features(all_minimum_features(),
                                                all_minimum_features_11()),
            0u);
}

/* ARM Mali-G720 (MediaTek MT6899 driver) reports every minimum feature except
 * vertexPipelineStoresAndAtomics and shaderClipDistance. Startup must accept it:
 * vertex-stage storage reads need neither stores nor atomics, and clip distance is
 * only used when viewport clipping planes are enabled. */
TEST(vulkan_minimum_features, mali_like_device_missing_vertex_stores_and_clip_distance_accepted)
{
  VkPhysicalDeviceFeatures features = all_minimum_features();
  features.vertexPipelineStoresAndAtomics = VK_FALSE;
  features.shaderClipDistance = VK_FALSE;
  EXPECT_EQ(GPU_vulkan_missing_minimum_features(features, all_minimum_features_11()), 0u);
}

TEST(vulkan_minimum_features, device_missing_fragment_stores_rejected)
{
  VkPhysicalDeviceFeatures features = all_minimum_features();
  features.fragmentStoresAndAtomics = VK_FALSE;
  const uint32_t missing = GPU_vulkan_missing_minimum_features(features,
                                                               all_minimum_features_11());
  EXPECT_NE(missing & uint32_t(GPUVulkanMinimumFeature::FragmentStoresAndAtomics), 0u);
}

TEST(vulkan_minimum_features, device_missing_dual_src_blend_rejected)
{
  VkPhysicalDeviceFeatures features = all_minimum_features();
  features.dualSrcBlend = VK_FALSE;
  const uint32_t missing = GPU_vulkan_missing_minimum_features(features,
                                                               all_minimum_features_11());
  EXPECT_NE(missing & uint32_t(GPUVulkanMinimumFeature::DualSrcBlend), 0u);
}

TEST(vulkan_minimum_features, device_missing_shader_draw_parameters_rejected)
{
  const uint32_t missing = GPU_vulkan_missing_minimum_features(all_minimum_features(),
                                                               VkPhysicalDeviceVulkan11Features{});
  EXPECT_NE(missing & uint32_t(GPUVulkanMinimumFeature::ShaderDrawParameters), 0u);
}

}  // namespace blender::gpu
