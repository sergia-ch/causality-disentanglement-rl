import gin
import numpy as np
import matplotlib
from itertools import product


@gin.configurable
def image_object_positions(h, w, positions,
                           colors):
    """Generate an image with objects at given positions.

    Args:
        h: height of the resulting image
        w: width of the resulting image
        positions: list of (x, y) coordinates for object positions
        colors: list of colors for objects, same length as positions
        n_objects:

    """
    assert len(positions) <= len(colors)
    n = len(positions)

    result = np.zeros((h, w, 3))
    colors_rgb = [matplotlib.colors.to_rgb(c) for c in colors]

    for p, c in zip(positions, colors_rgb[:n]):
        if 0 <= p[0] < h and 0 <= p[1] < w:
            result[p[0], p[1]] = c

    return result

@gin.configurable
def random_coordinates(h, w):
    """Get random coordinates inside the grid."""
    return (np.random.choice(h), np.random.choice(w))

@gin.configurable
def random_coordinates_n(n):
    """Get many random coordinates."""
    return [random_coordinates() for _ in range(n)]

@gin.configurable
def random_coordinates_n_no_overlap(h, w, n):
    """Generate random coordinates, without overlap."""
    all_pairs = list(product(range(h), range(w)))
    assert len(all_pairs) >= n, f"Too few pairs {all_pairs} {n} {h} {w}"
    idxes = np.random.choice(range(len(all_pairs)), n)
    return [all_pairs[idx] for idx in idxes]