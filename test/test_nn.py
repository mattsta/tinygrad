#!/usr/bin/env python
import unittest
import numpy as np
import torch
from tinygrad import Tensor, Device, TinyJit
from tinygrad.helpers import CI, Context
from tinygrad.nn import (
  BatchNorm2d,
  Conv1d,
  ConvTranspose1d,
  Conv2d,
  ConvTranspose2d,
  Linear,
  GroupNorm,
  LayerNorm,
  LayerNorm2d,
  Embedding,
  InstanceNorm,
)


@unittest.skipIf(CI and Device.DEFAULT == "CUDA", "slow")
class TestNN(unittest.TestCase):
  @unittest.skipIf(Device.DEFAULT == "WEBGPU", "no int64 on WebGPU")
  def test_sparse_cat_cross_entropy(self):
    # create in tinygrad
    input = Tensor.randn(5, 5)
    target = Tensor([0, 0, 0, 1, 2])  # torch doesn't support target=-1
    torch_input = torch.tensor(input.numpy())
    torch_target = torch.tensor(target.numpy(), dtype=torch.long)

    for smoothing in [0.0, 0.1, 0.5, 1.0]:
      for ignore_index in [-1, 0, 2]:
        loss = input.sparse_categorical_crossentropy(target, label_smoothing=smoothing, ignore_index=ignore_index)
        torch_loss = torch.nn.CrossEntropyLoss(reduction="mean", label_smoothing=smoothing, ignore_index=ignore_index)(torch_input, torch_target)
        np.testing.assert_allclose(loss.numpy(), torch_loss.detach().numpy(), atol=1e-5, rtol=1e-6)

  def test_batchnorm2d(self, training=False):
    with Tensor.train(training):
      szs = [4, 8, 16, 32]
      for sz in szs:
        # create in tinygrad
        bn = BatchNorm2d(sz, eps=1e-5, track_running_stats=training)
        bn.weight = Tensor.randn(sz)
        bn.bias = Tensor.randn(sz)
        bn.running_mean = Tensor.randn(sz)
        bn.running_var = Tensor.randn(sz)
        bn.running_var.numpy()[bn.running_var.numpy() < 0] = 0

        # create in torch
        with torch.no_grad():
          tbn = torch.nn.BatchNorm2d(sz).eval()
          tbn.training = training
          tbn.weight[:] = torch.tensor(bn.weight.numpy())
          tbn.bias[:] = torch.tensor(bn.bias.numpy())
          tbn.running_mean[:] = torch.tensor(bn.running_mean.numpy())
          tbn.running_var[:] = torch.tensor(bn.running_var.numpy())

        np.testing.assert_allclose(bn.running_mean.numpy(), tbn.running_mean.detach().numpy(), rtol=1e-5, atol=1e-6)
        np.testing.assert_allclose(bn.running_var.numpy(), tbn.running_var.detach().numpy(), rtol=1e-5, atol=1e-6)

        # trial
        inn = Tensor.randn(2, sz, 3, 3)

        # in tinygrad
        outt = bn(inn)

        # in torch
        toutt = tbn(torch.tensor(inn.numpy()))

        # close
        np.testing.assert_allclose(outt.numpy(), toutt.detach().numpy(), rtol=5e-4, atol=1e-6)
        np.testing.assert_allclose(bn.running_mean.numpy(), tbn.running_mean.detach().numpy(), rtol=1e-5, atol=1e-6)
        np.testing.assert_allclose(bn.running_var.numpy(), tbn.running_var.detach().numpy(), rtol=1e-5, atol=1e-6)

  def test_batchnorm2d_training(self):
    self.test_batchnorm2d(True)

  def test_batchnorm_axis(self):
    sz = (2, 4, 3, 2, 2)
    x = Tensor.randn(sz)
    weight = Tensor.randn(2, 3)
    bias = Tensor.randn(2, 3)
    mean = Tensor.randn(2, 3)
    invstd = Tensor.randn(2, 3)
    a = x.batchnorm(weight, bias, mean, invstd, axis=(0, 2)).permute(1, 0, 2, 3, 4).reshape(4, 6, 2, 2)
    b = x.permute(1, 0, 2, 3, 4).reshape(4, 6, 2, 2).batchnorm(weight.flatten(), bias.flatten(), mean.flatten(), invstd.flatten())
    t_x = torch.tensor(x.permute(1, 0, 2, 3, 4).reshape(4, 6, 2, 2).numpy())
    t_weight, t_bias = torch.tensor(weight.flatten().numpy()), torch.tensor(bias.flatten().numpy())
    t_mean, t_invstd = torch.tensor(mean.flatten().numpy()), torch.tensor(invstd.flatten().numpy())
    torch.nn.functional.batch_norm(t_x, t_mean, 1.0 / t_invstd**2, t_weight, t_bias)

    np.testing.assert_allclose(a.numpy(), b.numpy())

  def test_linear(self):
    def _test_linear(x, in_dim, out_dim):
      # create in tinygrad
      model = Linear(in_dim, out_dim)
      z = model(x)

      # create in torch
      with torch.no_grad():
        torch_layer = torch.nn.Linear(in_dim, out_dim).eval()
        torch_layer.weight[:] = torch.tensor(model.weight.numpy(), dtype=torch.float32)
        torch_layer.bias[:] = torch.tensor(model.bias.numpy(), dtype=torch.float32)
        torch_x = torch.tensor(x.numpy(), dtype=torch.float32)
        torch_z = torch_layer(torch_x)

      # test
      np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-4, rtol=1e-5)

    BS, T, in_dim, out_dim = 4, 2, 8, 16
    _test_linear(Tensor.randn(BS, in_dim), in_dim, out_dim)
    _test_linear(Tensor.randn(BS, T, in_dim), in_dim, out_dim)  # test with more dims

  def test_conv1d(self):
    BS, C1, W = 4, 16, 224 // 4
    C2, K, S, P = 64, 7, 2, 1

    # create in tinygrad
    layer = Conv1d(C1, C2, kernel_size=K, stride=S, padding=P)

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.Conv1d(C1, C2, kernel_size=K, stride=S, padding=P).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.uniform(BS, C1, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-4, rtol=1e-5)

  def test_conv2d(self):
    BS, C1, H, W = 4, 16, 224 // 4, 224 // 4
    C2, K, S, P = 64, 7, 2, 1

    # create in tinygrad
    layer = Conv2d(C1, C2, kernel_size=K, stride=S, padding=P)

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.Conv2d(C1, C2, kernel_size=K, stride=S, padding=P).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.uniform(BS, C1, H, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-4, rtol=1e-5)

  @unittest.skip("Takes too long to compile for Compiled backends")
  def test_conv2d_winograd(self):
    BS, C1, H, W = 2, 8, 16, 16
    C2, K, S, P = 8, 3, 1, 1

    # create in tinygrad
    layer = Conv2d(C1, C2, kernel_size=K, stride=S, padding=P)
    layer.weight.requires_grad = True
    layer.bias.requires_grad = True

    # create in torch
    torch_layer = torch.nn.Conv2d(C1, C2, kernel_size=K, stride=S, padding=P).eval()
    torch_layer.weight = torch.nn.Parameter(torch.tensor(layer.weight.numpy(), dtype=torch.float32))
    torch_layer.bias = torch.nn.Parameter(torch.tensor(layer.bias.numpy(), dtype=torch.float32))

    # test
    x = Tensor.uniform(BS, C1, H, W, requires_grad=True)

    with Context(WINO=1):
      z = layer(x)

    torch_x = torch.tensor(x.numpy(), requires_grad=True)
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-4, rtol=1e-5)

    m = z.mean()
    m.backward()
    gw = layer.weight.grad.realize()
    gb = layer.bias.grad.realize()
    gx = x.grad.realize()

    torch_z.mean().backward()
    np.testing.assert_allclose(gw.numpy(), torch_layer.weight.grad.numpy(), atol=5e-4, rtol=1e-5)
    np.testing.assert_allclose(gb.numpy(), torch_layer.bias.grad.numpy(), atol=5e-4, rtol=1e-5)
    np.testing.assert_allclose(gx.numpy(), torch_x.grad.numpy(), atol=5e-4, rtol=1e-5)

  @unittest.skipIf(CI and Device.DEFAULT == "WEBGPU", "runs out of memory in CI")
  def test_conv_transpose1d(self):
    BS, C1, W = 4, 16, 224 // 4
    C2, K, S, P = 64, 7, 2, 1

    # create in tinygrad
    layer = ConvTranspose1d(C1, C2, kernel_size=K, stride=S, padding=P)

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.ConvTranspose1d(C1, C2, kernel_size=K, stride=S, padding=P).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.uniform(BS, C1, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-4, rtol=1e-5)

  @unittest.skipIf(CI and Device.DEFAULT == "WEBGPU", "runs out of memory in CI")
  def test_conv_transpose2d(self):
    BS, C1, H, W = 4, 16, 224 // 4, 224 // 4
    C2, K, S, P = 64, 7, 2, 1

    # create in tinygrad
    layer = ConvTranspose2d(C1, C2, kernel_size=K, stride=S, padding=P)

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.ConvTranspose2d(C1, C2, kernel_size=K, stride=S, padding=P).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.uniform(BS, C1, H, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-4, rtol=1e-5)

  def test_groupnorm(self):
    BS, H, W, C, G = 20, 10, 10, 6, 3

    # create in tinygrad
    layer = GroupNorm(G, C)

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.GroupNorm(G, C).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.randn(BS, C, H, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-3, rtol=5e-3)

  def test_layernorm(self):
    N, C, H, W = 20, 5, 10, 10

    # create in tinygrad
    layer = LayerNorm([H, W])

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.LayerNorm([H, W]).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.randn(N, C, H, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-3, rtol=5e-3)

  def test_layernorm_2d(self):
    N, C, H, W = 20, 5, 10, 10

    # create in tinygrad
    layer = LayerNorm2d(C)

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.LayerNorm([C]).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.randn(N, C, H, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-3, rtol=5e-3)

  def test_instancenorm_2d(self):
    N, C, H, W = 20, 5, 10, 10

    # create in tinygrad
    layer = InstanceNorm(C)

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.InstanceNorm2d(C, affine=True).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.randn(N, C, H, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-3, rtol=5e-3)

  def test_instancenorm_3d(self):
    N, C, D, H, W = 20, 5, 3, 10, 10

    # create in tinygrad
    layer = InstanceNorm(C)

    # create in torch
    with torch.no_grad():
      torch_layer = torch.nn.InstanceNorm3d(C, affine=True).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)
      torch_layer.bias[:] = torch.tensor(layer.bias.numpy(), dtype=torch.float32)

    # test
    x = Tensor.randn(N, C, D, H, W)
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=5e-3, rtol=5e-3)

  def test_embedding(self):
    B, T, embed_size, vocab_size = 4, 10, 20, 28

    # create in tinygrad
    layer = Embedding(vocab_size, embed_size)

    with torch.no_grad():
      torch_layer = torch.nn.Embedding(vocab_size, embed_size).eval()
      torch_layer.weight[:] = torch.tensor(layer.weight.numpy(), dtype=torch.float32)

    # test
    x = Tensor(np.random.randint(0, vocab_size, (B, T)))
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=1e-8, rtol=1e-8)

    # test with empty input length
    x = Tensor(np.random.randint(0, vocab_size, (B, 0)))
    z = layer(x)
    torch_x = torch.tensor(x.numpy())
    torch_z = torch_layer(torch_x)
    np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=1e-8, rtol=1e-8)

    # test with jit enabled
    @TinyJit
    def layer_jit(x):
      return layer(x).realize()

    for _ in range(3):
      x = Tensor(np.random.randint(0, vocab_size, (B, T)))
      z = layer_jit(x)
      torch_x = torch.tensor(x.numpy())
      torch_z = torch_layer(torch_x)
      np.testing.assert_allclose(z.numpy(), torch_z.detach().numpy(), atol=1e-8, rtol=1e-8)


if __name__ == "__main__":
  unittest.main()
