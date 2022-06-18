from tinygrad.shapetracker import ShapeTracker
from collections import namedtuple
import functools
import numpy as np
import sys
sys.setrecursionlimit(10000)

# TODO: these aren't really ops
from typing import Union, NamedTuple, List, Any, Tuple
from tinygrad.ops import ReduceOps, BinaryOps, MovementOps, ProcessingOps, UnaryOps, log_op
Op = Union[UnaryOps, BinaryOps, ReduceOps, MovementOps, ProcessingOps]
ElementWiseOps = Union[UnaryOps, BinaryOps]
OpTypes = Union[ElementWiseOps, ReduceOps, MovementOps, ProcessingOps]

def tp(a,b,c=[]): return tuple(list(a) + list(b) + list(c))

# movement ops can be moved above elementwise ops 
SHUFFLE_MOVEMENT_OPS = True

# sequential movement ops can be flattened into 0 or 1 movement ops 
MERGE_MOVEMENT_OPS = True

# if you stick the right movement ops, they might disappear!
# TODO: this is wrong
REMOVE_MOVEMENT_NOPS = False

# "sequential" elementwise ops can be merged into 1 big elementwise op
MERGE_ELEMENTWISE_OPS = True

# after the conv is done, it can run elementwise ops on its output
MERGE_ELEMENTWISE_INTO_CONV_OUTPUT = True

class LazyOp(NamedTuple):
  op: Op
  src: List # LazyOp or LazyBuffer
  arg: Any = None

def get_lazybuffers(op:LazyOp):
  #print(op)
  ret = []
  for x in op.src:
    if isinstance(x, LazyOp):
      ret += get_lazybuffers(x)
    elif isinstance(x, LazyBuffer):
      ret.append(x)
    else:
      raise Exception("wtf")
  return ret

def get_lazyops(op:LazyOp):
  ret = [op.op]
  for x in op.src:
    if isinstance(x, LazyOp):
      ret += get_lazyops(x)
    elif isinstance(x, LazyBuffer):
      pass
    else:
      raise Exception("wtf")
  return ret

class LazyBuffer:
  def __init__(self, shape:tuple, optype:OpTypes, op:LazyOp):
    assert isinstance(op, LazyOp)
    assert isinstance(op.src, list)
    assert isinstance(shape, tuple)

    #print(shape, optype, op)
    self.shape = shape
    self.optype = optype
    self.dtype = np.float32
    self.op = op
    self.did_realize = False

  def realize(self):
    if self.did_realize or self.optype is None: return self
    self.did_realize = True
    srcs = [s.realize() for s in get_lazybuffers(self.op)]
    # TODO: do real op here
    log_op(self.optype.__name__, get_lazyops(self.op), self, srcs)
    return self

  @staticmethod
  def fromCPU(x):
    return LazyBuffer(x.shape, None, LazyOp(None, [], x))

  def toCPU(self):
    self.realize()
    # this realizes the tensor 
    return np.zeros(self.shape, self.dtype)

@functools.lru_cache()
def elementwise_op(op, srcs:Tuple[LazyBuffer]) -> LazyBuffer:
  out_shape = srcs[0].shape
  if MERGE_ELEMENTWISE_INTO_CONV_OUTPUT:
    # TODO: this is wrong
    cnt = sum([x.optype == ProcessingOps for x in srcs])
    if cnt == 1:
      srcs = [x.op if x.optype == ProcessingOps else x for x in srcs]
      return LazyBuffer(out_shape, ProcessingOps, LazyOp(op, srcs))
    elif cnt == 2:
      # have to confirm they are the same conv
      def find_conv(x:LazyOp):
        if isinstance(x, LazyBuffer):
          return None
        if isinstance(x.op, ProcessingOps):
          return x
        for s in x.src:
          tst = find_conv(s)
          if tst is not None:
            return tst
        return None
      c1 = find_conv(srcs[0].op)
      c2 = find_conv(srcs[1].op)
      #print(c1.op, c2.op)
      if c1.arg == c2.arg and tuple(c1.src) == tuple(c2.src):
        srcs = [x.op if x.optype == ProcessingOps else x for x in srcs]
        return LazyBuffer(out_shape, ProcessingOps, LazyOp(op, srcs))
      else:
        # mismatch convs, don't merge
        pass

  if MERGE_ELEMENTWISE_OPS:
    srcs = [x.op if x.optype == BinaryOps else x for x in srcs]
  return LazyBuffer(out_shape, BinaryOps, LazyOp(op, list(srcs)))

# caching is safe here, the same op and arg applied to the same buffer is the same
@functools.lru_cache()
def movement_op(op:MovementOps, x:LazyBuffer, arg) -> LazyBuffer:
  st = ShapeTracker(*x.shape)
  st = st.movement_op(op, arg)
  if len(st.views) == 1: return x    # this is a no-op

  if SHUFFLE_MOVEMENT_OPS:
    if x.optype == ElementWiseOps:
      def replace_w_movement_op(y:Union[LazyOp, LazyBuffer]):
        if isinstance(y, LazyBuffer):
          return movement_op(op, y, arg)
        elif isinstance(y, LazyOp):
          return LazyOp(y.op, [replace_w_movement_op(z) for z in y.src], y.arg)
      return LazyBuffer(st.shape, ElementWiseOps, replace_w_movement_op(x.op))

  if REMOVE_MOVEMENT_NOPS:
    if x.optype == MovementOps:
      root = x.op
      op_arg = [(op, arg)]
      while isinstance(root, LazyOp):
        op_arg.append((root.op, root.arg))
        root = root.src[0]
      assert isinstance(root, LazyBuffer)
      rst = ShapeTracker(*root.shape)
      for o,a in op_arg[::-1]:
        rst = rst.movement_op(o, a)
      # TODO: this check is wrong, we used the shapetracker for a reason
      if rst.shape == root.shape:
        return root

  if MERGE_MOVEMENT_OPS:
    if isinstance(x.op.op, MovementOps):
      x = x.op

  # otherwise just create the movement op
  return LazyBuffer(st.shape, MovementOps, LazyOp(op, [x], arg))

def reduce_op(op, x, new_shape): return LazyBuffer(new_shape, ReduceOps, LazyOp(op, [x], new_shape))
def processing_op(op, x, w, C): return LazyBuffer(C.out_shape, ProcessingOps, LazyOp(op, [x, w], C))

# universal dispatcher?
class Ops:
  def unary_op(ctx, op, x): return elementwise_op(op, (x,))
  def binary_op(ctx, op, x, y): return elementwise_op(op, (x,y))
  def movement_op(ctx, op, x, arg): return movement_op(op, x, tuple(arg))
  # blocker ops
  def reduce_op(ctx, op, x, new_shape): return reduce_op(op, x, new_shape)
  def processing_op(ctx, op, x, w, C): return processing_op(op, x, w, C)
