# Copyright 2021 AlQuraishi Laboratory
# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import torch
import torch.nn as nn
from typing import Dict

import openfold.np.residue_constants as rc 
from openfold.utils.affine_utils import T
from openfold.utils.tensor_utils import (
    batched_gather, 
    one_hot,
    tree_map,
    tensor_tree_map,
)


def pseudo_beta_fn(aatype, all_atom_positions, all_atom_masks):
    is_gly = (aatype == rc.restype_order['G'])
    ca_idx = rc.atom_order['CA']
    cb_idx = rc.atom_order['CB']
    pseudo_beta = torch.where(
        is_gly[..., None].expand(*((-1,) * len(is_gly.shape)), 3),
        all_atom_positions[..., ca_idx, :],
        all_atom_positions[..., cb_idx, :]
    )

    if(all_atom_masks is not None):
        pseudo_beta_mask = torch.where(
            is_gly,
            all_atom_masks[..., ca_idx],
            all_atom_masks[..., cb_idx],
        )
        return pseudo_beta, pseudo_beta_mask
    else:
        return pseudo_beta


def atom14_to_atom37(atom14, batch):
    atom37_data = batched_gather(
        atom14,
        batch["residx_atom37_to_atom14"],
        dim=-2,
        no_batch_dims=len(atom14.shape[:-2]),
    )

    atom37_data = atom37_data * batch["atom37_atom_exists"][..., None]

    return atom37_data


def build_template_angle_feat(template_feats):
    template_aatype = template_feats["template_aatype"]
    torsion_angles_sin_cos = template_feats["template_torsion_angles_sin_cos"]
    alt_torsion_angles_sin_cos = (
        template_feats["template_alt_torsion_angles_sin_cos"]
    )
    torsion_angles_mask = template_feats["template_torsion_angles_mask"]
    template_angle_feat = torch.cat(
        [
            nn.functional.one_hot(template_aatype, 22),
            torsion_angles_sin_cos.reshape(
                *torsion_angles_sin_cos.shape[:-2], 14
            ),
            alt_torsion_angles_sin_cos.reshape(
                *alt_torsion_angles_sin_cos.shape[:-2], 14
            ),
            torsion_angles_mask,
        ], 
        dim=-1,
    )
    
    return template_angle_feat


def build_template_pair_feat(batch, min_bin, max_bin, no_bins, eps=1e-20, inf=1e8):
    template_mask = batch["template_pseudo_beta_mask"]
    template_mask_2d = template_mask[..., None] * template_mask[..., None, :]

    # Compute distogram (this seems to differ slightly from Alg. 5)
    tpb = batch["template_pseudo_beta"]
    dgram = torch.sum(
        (tpb[..., None, :] - tpb[..., None, :, :]) ** 2, dim=-1, keepdim=True)
    lower = torch.linspace(min_bin, max_bin, no_bins, device=tpb.device) ** 2
    upper = torch.cat([lower[:-1], lower.new_tensor([inf])], dim=-1)
    dgram = ((dgram > lower) * (dgram < upper)).type(dgram.dtype)

    to_concat = [dgram, template_mask_2d[..., None]]

    aatype_one_hot = nn.functional.one_hot(
        batch["template_aatype"], rc.restype_num + 2, 
    )

    n_res = batch["template_aatype"].shape[-1]
    to_concat.append(
        aatype_one_hot[..., None, :, :].expand(
            *aatype_one_hot.shape[:-2], n_res, -1, -1
        )
    )
    to_concat.append(
        aatype_one_hot[..., None, :].expand(
            *aatype_one_hot.shape[:-2], -1, n_res, -1
        )
    )

    n, ca, c = [rc.atom_order[a] for a in ['N', 'CA', 'C']]
    # TODO: Consider running this in double precision
    affines = T.make_transform_from_reference(
        n_xyz=batch["template_all_atom_positions"][..., n, :],
        ca_xyz=batch["template_all_atom_positions"][..., ca, :],
        c_xyz=batch["template_all_atom_positions"][..., c, :],
        eps=eps,
    )

    points = affines.get_trans()[..., None, :, :]
    affine_vec = affines[..., None].invert_apply(points)
     
    inv_distance_scalar = torch.rsqrt(
        eps + torch.sum(affine_vec ** 2, dim=-1)
    )

    t_aa_masks = batch["template_all_atom_mask"]
    template_mask = (
        t_aa_masks[..., n] * t_aa_masks[..., ca] * t_aa_masks[..., c]
    )
    template_mask_2d = template_mask[..., None] * template_mask[..., None, :]

    inv_distance_scalar = inv_distance_scalar * template_mask_2d
    unit_vector = (affine_vec * inv_distance_scalar[..., None])
    to_concat.extend(torch.unbind(unit_vector[..., None, :], dim=-1))
    to_concat.append(template_mask_2d[..., None])
   
    act = torch.cat(to_concat, dim=-1)
    act = act * template_mask_2d[..., None]

    return act


def build_extra_msa_feat(batch):
    msa_1hot = nn.functional.one_hot(batch["extra_msa"], 23)
    msa_feat = [
        msa_1hot,
        batch["extra_has_deletion"].unsqueeze(-1),
        batch["extra_deletion_value"].unsqueeze(-1),
    ]
    return torch.cat(msa_feat, dim=-1)


# adapted from model/tf/data_transforms.py
def build_msa_feat(batch):
  """Create and concatenate MSA features."""
  # Whether there is a domain break. Always zero for chains, but keeping
  # for compatibility with domain datasets.
  has_break = batch["between_segment_residues"] 
  aatype_1hot = nn.functional.one_hot(batch['aatype'], num_classes=21)

  target_feat = [
      has_break.unsqueeze(-1),
      aatype_1hot,  # Everyone gets the original sequence.
  ]

  msa_1hot = nn.functional.one_hot(batch['msa'], num_classes=23)
  has_deletion = batch["deletion_matrix"]
  deletion_value = torch.atan(batch['deletion_matrix'] / 3.) * (2. / math.pi)

  msa_feat = [
      msa_1hot,
      has_deletion.unsqueeze(-1),
      deletion_value.unsqueeze(-1),
  ]

  if 'cluster_profile' in batch:
    deletion_mean_value = (
        tf.atan(batch['cluster_deletion_mean'] / 3.) * (2. / np.pi))
    msa_feat.extend([
        batch['cluster_profile'],
        tf.expand_dims(deletion_mean_value, axis=-1),
    ])

  if 'extra_deletion_matrix' in protein:
    batch['extra_has_deletion'] = tf.clip_by_value(
        batch['extra_deletion_matrix'], 0., 1.)
    batch['extra_deletion_value'] = tf.atan(
        batch['extra_deletion_matrix'] / 3.) * (2. / np.pi)

  batch['msa_feat'] = torch.cat(msa_feat, dim=-1)
  batch['target_feat'] = torch.cat(target_feat, dim=-1)
  return batch


def torsion_angles_to_frames(
    t: T, 
    alpha: torch.Tensor, 
    aatype: torch.Tensor, 
    rrgdf: torch.Tensor,
):
    # [*, N, 8, 4, 4]
    default_4x4 = rrgdf[aatype, ...]
    
    # [*, N, 8] transformations, i.e.
    #   One [*, N, 8, 3, 3] rotation matrix and
    #   One [*, N, 8, 3]    translation matrix
    default_t = T.from_4x4(default_4x4)

    bb_rot = alpha.new_zeros((*((1,) * len(alpha.shape[:-1])), 2))
    bb_rot[..., 1] = 1
    
    # [*, N, 8, 2]
    alpha = torch.cat(
        [bb_rot.expand(*alpha.shape[:-2], -1, -1), alpha], 
        dim=-2
    )

    # [*, N, 8, 3, 3]
    # Produces rotation matrices of the form:
    # [
    #   [1, 0  , 0  ],
    #   [0, a_2,-a_1],
    #   [0, a_1, a_2]
    # ]
    # This follows the original code rather than the supplement, which uses
    # different indices.
        
    all_rots = alpha.new_zeros(default_t.rots.shape)
    all_rots[..., 0, 0] = 1
    all_rots[..., 1, 1] = alpha[..., 1]
    all_rots[..., 1, 2] = -alpha[..., 0]
    all_rots[..., 2, 1:] = alpha

    all_rots = T(all_rots, None)

    all_frames = default_t.compose(all_rots)

    chi2_frame_to_frame = all_frames[..., 5]
    chi3_frame_to_frame = all_frames[..., 6]
    chi4_frame_to_frame = all_frames[..., 7]

    chi1_frame_to_bb = all_frames[..., 4]
    chi2_frame_to_bb = chi1_frame_to_bb.compose(chi2_frame_to_frame)
    chi3_frame_to_bb = chi2_frame_to_bb.compose(chi3_frame_to_frame)
    chi4_frame_to_bb = chi3_frame_to_bb.compose(chi4_frame_to_frame)

    all_frames_to_bb = T.concat([
            all_frames[..., :5],
            chi2_frame_to_bb.unsqueeze(-1),
            chi3_frame_to_bb.unsqueeze(-1),
            chi4_frame_to_bb.unsqueeze(-1),
        ], dim=-1,
    )

    all_frames_to_global = t[..., None].compose(all_frames_to_bb)

    return all_frames_to_global


def frames_and_literature_positions_to_atom14_pos(
    t: T,
    aatype: torch.Tensor,
    default_frames,
    group_idx,
    atom_mask,
    lit_positions,
):
    # [*, N, 14, 4, 4] 
    default_4x4 = default_frames[aatype, ...]
    
    # [*, N, 14]
    group_mask = group_idx[aatype, ...]
    
    # [*, N, 14, 8]
    group_mask = nn.functional.one_hot(
        group_mask, num_classes=default_frames.shape[-3],
    )

    # [*, N, 14, 8]
    t_atoms_to_global = t[..., None, :] * group_mask
    
    # [*, N, 14]
    t_atoms_to_global = t_atoms_to_global.map_tensor_fn(
        lambda x: torch.sum(x, dim=-1)
    )

    # [*, N, 14, 1]
    atom_mask = atom_mask[aatype, ...].unsqueeze(-1)

    # [*, N, 14, 3]
    lit_positions = lit_positions[aatype, ...]
    pred_positions = t_atoms_to_global.apply(lit_positions)
    pred_positions = pred_positions * atom_mask

    return pred_positions
