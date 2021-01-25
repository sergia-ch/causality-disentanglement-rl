import torch
import gin

def contrastive_loss_permute(pair_a, pair_b, fcn):
    """Contrastive loss by permuting pair_a."""
    assert pair_a.shape[0] == pair_b.shape[0], (pair_a.shape, pair_b.shape)
    batch_dim = pair_a.shape[0]

    criterion = torch.nn.BCEWithLogitsLoss(reduction='mean')

    # original inputs order
    idxes_orig = torch.arange(start=0, end=batch_dim).to(pair_a.device)

    # random permutation for incorrect inputs
    idxes = torch.randperm(batch_dim).to(pair_a.device)
    pair_a_shuffled = pair_a[idxes]

    # correct pairs for contrastive loss
    logits_true_correct = fcn(pair_a=pair_a, pair_b=pair_b)
    target_correct = torch.ones([batch_dim, ], dtype=torch.float32)

    # incorrect pairs, contrastive loss
    logits_true_incorrect = fcn(pair_a=pair_a_shuffled, pair_b=pair_b)
    target_incorrect = (idxes == idxes_orig).to(torch.float32).to(pair_a.device)

    # two parts of the loss
    loss_correct = criterion(logits_true_correct.view(-1), target_correct)
    loss_incorrect = criterion(logits_true_incorrect.view(-1), target_incorrect)

    return loss_correct + loss_incorrect


@gin.configurable
def decoder_discriminator_loss(obs, decoder, decoder_discriminator, **kwargs):
    """True value is not 0, sometimes will get the same input..."""

    def fcn(pair_a, pair_b):
        # pair_a == obs
        return decoder_discriminator(o_t=pair_a, f_t=pair_b)

    return contrastive_loss_permute(obs, decoder(obs), fcn)