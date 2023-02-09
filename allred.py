import os
import torch
import torch.distributed as dist
from functools import partial
from torch._dynamo.utils import same
from torch._inductor.compile_fx import compile_fx as inductor_compile_fx
from torch.fx.experimental.proxy_tensor import make_fx
# from torch._C._distributed_c10d import _register_process_group
from torch._dispatch.python import enable_python_dispatcher

# LOL if you don't remember to import this, then the op isn't registered and it hits
# the no-op C++ kernel that i am forced to implement despite not using it
import torch.distributed.traceable_collectives

def matmul_cat_col(a, b, c, d, e, f, *, tag, ranks, group_size):
    x = torch.matmul(a, b)
    y = torch.matmul(c, d)
    z = torch.cat((x, y))
    ar = torch.ops.aten.all_reduce(z, "sum", tag, ranks, group_size)
    g = torch.matmul(e, f)
    ar = torch.ops.tr_c10d.wait(ar)
    out = torch.add(ar, g.repeat(2, 1))
    return (out, )

def compile(func, example_inputs):
    graph = make_fx(func)(*example_inputs)
    return inductor_compile_fx(graph, example_inputs)

if __name__ == '__main__':
    os.environ["RANK"] = os.getenv("RANK", "0")
    os.environ["WORLD_SIZE"] = os.getenv("WORLD_SIZE", "1")
    os.environ["MASTER_ADDR"] = os.getenv("MASTER_ADDR", "localhost")
    os.environ["MASTER_PORT"] = os.getenv("MASTER_PORT", "12345")
    rank = int(os.getenv("RANK"))
    world_size = int(os.getenv("WORLD_SIZE"))
    torch.cuda.set_device(rank)
    dist.init_process_group(backend='nccl')

    torch._inductor.config.debug = True

    matmul_cat_col = partial(matmul_cat_col, tag="", ranks=list(range(world_size)), group_size=world_size)
    inputs = (torch.ones(4, 4, device="cuda") + rank,) * 6

    # non-ideally, i seem to need to enable this at user level in order to construct a torchdispatch subclass
    # inside py registered collective ops
    with enable_python_dispatcher():
        correct_out = matmul_cat_col(*inputs)
        compiled_matmul_cat_col = compile(matmul_cat_col, inputs)
        inductor_out = compiled_matmul_cat_col(*inputs)
        print(f"rank {rank}: {correct_out}, {inductor_out}")
        assert same(correct_out, inductor_out)
