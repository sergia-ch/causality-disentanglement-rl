import gin
import torch
from . import l1_ball_projection_pytorch
l1_ball_projection_pytorch.torch = torch


@gin.configurable
def switch_project_l1(model, max_l1_norm=1, **kwargs):
    """Perform a projection of the switch parameters."""
    if not hasattr(model, 'switch__params'):
        raise ValueError("Only support models with switch__params")

    swp = list(model.switch__params)
    if len(swp) != 1:
        raise ValueError("Only support 1 parameter tensor")

    parameter = swp[0]
    parameter.data = l1_ball_projection_pytorch.project_onto_l1_ball(
        x=parameter.data.unsqueeze(0), eps=max_l1_norm).detach()[0]

    # no gradient!
    return {'loss': torch.sum(torch.abs(parameter.data)).detach(),
            'metrics': {'coeff': max_l1_norm}}
