import unittest, time, gc
import numpy as np
from tinygrad.tensor import Tensor
from tinygrad.nn import optim
from tinygrad.nn.state import get_parameters
from tinygrad.features.jit import TinyJit
from tinygrad import Device, GlobalCounters, dtypes
from tinygrad.helpers import CI
from tinygrad.shape.symbolic import Variable
from extra.lr_scheduler import OneCycleLR
from test.helpers import derandomize_model

from examples.gpt2 import Transformer as GPT2Transformer, MODEL_PARAMS as GPT2_MODEL_PARAMS
from examples.hlb_cifar10 import SpeedyResNet, hyp
from examples.llama import Transformer as LLaMaTransformer, MODEL_PARAMS as LLAMA_MODEL_PARAMS
from examples.stable_diffusion import UNetModel, ResBlock

global_mem_used = 0


def helper_test(nm, gen, train, max_memory_allowed, max_kernels_allowed, all_jitted=False):
  tms = []
  for _ in range(4):
    early_gen = [x.realize() if isinstance(x, Tensor) else x for x in gen()]
    GlobalCounters.reset()
    Device[Device.DEFAULT].synchronize()
    st = time.perf_counter_ns()
    train(*early_gen)
    Device[Device.DEFAULT].synchronize()
    tms.append(time.perf_counter_ns() - st)
  mem_used = GlobalCounters.mem_used - global_mem_used

  # TODO: jit should expose this correctly with graph
  kernels_used = len(train.jit_cache) if hasattr(train, "jit_cache") else None
  print(f"{nm}: used {mem_used / 1e9:.2f} GB and {kernels_used} kernels in {min(tms) / 1e6:.2f} ms")
  assert mem_used / 1e9 < max_memory_allowed, f"{nm} used more than {max_memory_allowed:.2f} GB"
  assert not kernels_used or kernels_used <= max_kernels_allowed, f"{nm} used more than {max_kernels_allowed} kernels"
  if all_jitted:
    assert (
      kernels_used > 0
      and kernels_used == GlobalCounters.kernel_count
      or (kernels_used <= GlobalCounters.kernel_count and getattr(Device[Device.DEFAULT], "graph", None))
    ), f"only {kernels_used} out of {GlobalCounters.kernel_count} were jitted"  # noqa: E501


class TestRealWorld(unittest.TestCase):
  def setUp(self):
    gc.collect()
    global global_mem_used
    global_mem_used = GlobalCounters.mem_used
    self.old_float = dtypes.default_float
    np.random.seed(2002)

  def tearDown(self):
    dtypes.default_float = self.old_float

  @unittest.skipIf(Device.DEFAULT == "LLVM", "LLVM segmentation fault")
  @unittest.skipIf(CI, "too big for CI")
  def test_stable_diffusion(self):
    model = UNetModel()
    derandomize_model(model)

    @TinyJit
    def test(t, t2):
      return model(t, 801, t2).realize()

    helper_test("test_sd", lambda: (Tensor.randn(1, 4, 64, 64), Tensor.randn(1, 77, 768)), test, 18.0, 953)

  def test_mini_stable_diffusion(self):
    model = [ResBlock(16, 24, 16) for _ in range(4)]
    derandomize_model(model)

    @TinyJit
    def test(t, t2):
      for l in model:
        t = l(t, t2)
      return t.realize()

    helper_test("test_mini_sd", lambda: (Tensor.empty(4, 16, 8, 8), Tensor.empty(1, 24)), test, 0.01, 43)

  @unittest.skipIf(Device.DEFAULT == "LLVM", "LLVM segmentation fault")
  @unittest.skipIf(Device.DEFAULT in ["LLVM", "GPU"] and CI, "too long on CI LLVM, GPU requires cl_khr_fp16")
  def test_llama(self):
    dtypes.default_float = dtypes.float16

    args_tiny = {"dim": 1024, "hidden_dim": 2048, "n_heads": 8, "n_layers": 8, "norm_eps": 1e-05, "vocab_size": 1000}
    model = LLaMaTransformer(**(args_tiny if CI else LLAMA_MODEL_PARAMS["1"]["7B"]["args"]))
    derandomize_model(model)

    @TinyJit
    def test(t):
      return model(t, 0).realize()

    # TODO: test first token vs rest properly
    helper_test("test_llama", lambda: (Tensor([[1, 2, 3, 4]]),), test, 0.27 if CI else 14.9, 192 if CI else 719, all_jitted=True)

  @unittest.skipIf(Device.DEFAULT in ["LLVM", "GPU"] and CI, "too long on CI LLVM, GPU requires cl_khr_fp16")
  def test_gpt2(self):
    dtypes.default_float = dtypes.float16

    args_tiny = {"dim": 1024, "n_heads": 8, "n_layers": 8, "norm_eps": 1e-5, "vocab_size": 1000}
    model = GPT2Transformer(**(args_tiny if CI else GPT2_MODEL_PARAMS["gpt2-medium"]))
    derandomize_model(model)

    @TinyJit
    def test(t, v):
      return model(t, v).realize()

    helper_test(
      "test_gpt2",
      lambda: (
        Tensor([
          [
            1,
          ]
        ]),
        Variable("pos", 1, 100).bind(1),
      ),
      test,
      0.23 if CI else 0.9,
      164 if CI else 468,
      all_jitted=True,
    )

  def test_train_mnist(self):
    from examples.beautiful_mnist import Model

    with Tensor.train():
      model = Model()
      optimizer = optim.Adam(get_parameters(model))
      BS = 32

      @TinyJit
      def train(X):
        out = model(X)
        loss = out.mean()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

      helper_test("train_mnist", lambda: (Tensor.randn(BS, 1, 28, 28),), train, 0.07, 127)

  @unittest.skipIf(Device.DEFAULT == "LLVM", "LLVM segmentation fault")
  @unittest.skipIf(Device.DEFAULT in ["LLVM", "CLANG"] and CI, "too long on CI LLVM and CLANG")
  def test_train_cifar(self):
    # TODO: with default device
    # old_default = Device.DEFAULT
    # Device.DEFAULT = "FAKE"
    # Device['fake'].codegen = Device[old_default].codegen

    with Tensor.train():
      model = SpeedyResNet(Tensor.ones((12, 3, 2, 2)))
      optimizer = optim.SGD(get_parameters(model), lr=0.01, momentum=0.8, nesterov=True, weight_decay=0.15)

      BS = 32 if CI else 512

      @TinyJit
      def train(X):
        out = model(X)
        loss = out.mean()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

      helper_test("train_cifar", lambda: (Tensor.randn(BS, 3, 32, 32),), train, (1.0 / 48) * BS, 142 if CI else 154)  # it's 154 on metal

      # reset device
      # Device.DEFAULT = old_default

  @unittest.skipIf(Device.DEFAULT == "LLVM", "LLVM segmentation fault")
  @unittest.skipIf(Device.DEFAULT in ["GPU"] and CI, "opencl on intel can't compile half")
  def test_train_cifar_hyp(self):
    dtypes.default_float = dtypes.float16
    with Tensor.train():
      model = SpeedyResNet(Tensor.ones((12, 3, 2, 2)))
      optimizer = optim.SGD(get_parameters(model), lr=0.01, momentum=hyp["opt"]["momentum"], nesterov=True, weight_decay=hyp["opt"]["bias_decay"])
      initial_div_factor = hyp["opt"]["initial_div_factor"]
      final_lr_ratio = hyp["opt"]["final_lr_ratio"]
      pct_start = hyp["opt"]["percent_start"]
      lr_scheduler = OneCycleLR(
        optimizer,
        max_lr=hyp["opt"]["bias_lr"],
        pct_start=pct_start,
        div_factor=initial_div_factor,
        final_div_factor=1.0 / (initial_div_factor * final_lr_ratio),
        total_steps=4,
      )
      assert not np.isnan(lr_scheduler.min_lr), "lr too small or initial_div_facotr too big for half"

    dtypes.default_float = dtypes.float32


if __name__ == "__main__":
  unittest.main()
