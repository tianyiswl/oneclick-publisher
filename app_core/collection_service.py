"""演示占位。"""
def __getattr__(_name):
    return lambda *_args, **_kwargs: []
