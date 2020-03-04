import random
from typing import Tuple

import torch

from allennlp.data import TextFieldTensors
import torch.distributed as dist


def get_world_size() -> int:
    """Returns the world size, or -1 if `torch.distributed.init_process_group` was never called.
    """
    try:
        return dist.get_world_size()
    except AssertionError:
        return -1


def sample_anchor_positive_pairs(tokens) -> Tuple[TextFieldTensors, TextFieldTensors]:
    """Returns a tuple of `TextFieldTensors` containing random batches of anchors and positives from tokens.

    # Parameters

    tokens : TextFieldTensors
        From a `TextField`

    anchors : TextFieldTensors
        `TextFieldTensors` containing the sampled anchors.
    positives : TextFieldTensors
        `TextFieldTensors` containing the sampled positives.
    """
    # The procedure for sampling anchor, positive pairs is as follows:
    #   1. Sample two random spans for every training instance
    #   2. Unpack the TextFieldTensors, extract the token ids, masks, and type ids for the sampled pairs
    #   3. Repackage the information into TextFieldTensors
    num_spans = tokens["tokens"]["token_ids"].size(1)
    index = torch.as_tensor(random.sample(range(0, num_spans), 2), device=tokens["tokens"]["token_ids"].device,)

    random_token_ids = torch.index_select(tokens["tokens"]["token_ids"], dim=1, index=index)
    random_masks = torch.index_select(tokens["tokens"]["mask"], dim=1, index=index)
    random_type_ids = torch.index_select(tokens["tokens"]["type_ids"], dim=1, index=index)

    anchor_token_ids, positive_token_ids = torch.chunk(random_token_ids, 2, dim=1)
    anchor_masks, positive_masks = torch.chunk(random_masks, 2, dim=1)
    anchor_type_ids, positive_type_ids = torch.chunk(random_type_ids, 2, dim=1)

    anchors: TextFieldTensors = {
        "tokens": {
            "token_ids": anchor_token_ids.squeeze(1),
            "mask": anchor_masks.squeeze(1),
            "type_ids": anchor_type_ids.squeeze(1),
        }
    }
    positives: TextFieldTensors = {
        "tokens": {
            "token_ids": positive_token_ids.squeeze(1),
            "mask": positive_masks.squeeze(1),
            "type_ids": positive_type_ids.squeeze(1),
        }
    }

    return anchors, positives


def all_gather_anchor_positive_pairs(
    anchors: torch.Tensor, positives: torch.Tensor
) -> Tuple[torch.Tensor, torch.Tensor]:
    """If training on 2 or more GPUs, `all_gather`s the embeddings produced on each replica, ensuring that the
    gradients for the embeddings produced on each replica are not lost. The returned anchor, positive pairs can be
    fed to a contrastive loss. This method is necessary to ensure that we train against the expected number of
    negatives 2 * (batch size - 1) per batch, as a naive implementation would end up training against
    2 * (batch size / n_gpus - 1) number of negatives. If we are not training on 2 or more GPUs, this method is a
    no-op and returns its inputs.

    # Parameters

    anchors : torch.Tensor
        Embedded text representing the anchors.
    positives : TextFieldTensors
        Embedded text representing the positives.

    # Returns

    Tuple[torch.Tensor, torch.Tensor]
    Embedded anchor, positive pairs that can be fed to a contrastive loss.
    """

    # If we are not training on at least 2 GPUs, this is a no-op.
    if get_world_size() < 2:
        return anchors, positives

    # Gather the encoded anchors and positives on all replicas
    anchors_list = [torch.ones_like(anchors) for _ in range(dist.get_world_size())]
    positives_list = [torch.ones_like(positives) for _ in range(dist.get_world_size())]
    dist.all_gather(anchors_list, anchors)
    dist.all_gather(positives_list, positives)
    # The gathered copy of the current replicas positive pairs have no gradients, so we overwrite them
    # with the positive pairs generated on this replica, which DO have gradients back to the encoder.
    anchors_list[dist.get_rank()] = anchors
    positives_list[dist.get_rank()] = positives
    # Finally, we concatenate the positive pairs so they can be fed to the contrastive loss
    anchors = torch.cat(anchors_list)
    positives = torch.cat(positives_list)

    return anchors, positives
