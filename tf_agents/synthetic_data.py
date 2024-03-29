import numpy as np

# "model of the environment"
A = np.array([
    [1,0,1,0], # state and action, gives s_1
    [0,1,0,1],
])


# number of actions
n_a = 5

# number of state components
n_s = 5

# number of observation components
n_o = 5

# number of features
n_f = 5

A = np.concatenate([np.eye(n_s), np.eye(n_a)], axis=1)


# transform for the state
Q1 = np.random.randn(n_o, n_s)

# transform for state+action
Q = np.eye(n_o * 2)
Q[:n_s, :n_s] = Q1

# number of data pts
N = 1000

# states
xs = np.random.randn(n_s + n_o, N)

# next states
ys = A @ xs

xs_e = Q @ xs
ys_e = Q1 @ ys

# states
xs = xs.T
ys = ys.T

# observations
xs_e = xs_e.T
ys_e = ys_e.T

# check dimensions
assert xs.shape[1] == n_s + n_a
assert ys.shape[1] == n_s
assert xs_e.shape[1] == n_o + n_a
assert ys_e.shape[1] == n_o

# checking that the model works correctly
assert np.allclose(A @ (np.linalg.inv(Q) @ xs_e[0]), ys[0])
assert np.allclose(xs_e[0][n_s:], xs[0][n_s:])
