# -*- coding: utf-8 -*-
"""
诊断脚本 04：完整 main() dry-run
- 启动 联网_service.main()，但在 _attach 完成后立刻抛 KeyboardInterrupt
- 不真正 bind Web UI 端口
- 捕获 main() 启动路径上任何异常
"""
import sys, os, traceback, threading, time

sys.path.insert(0, '.')

# 关键：把 stdin 关掉，让 main() 不会因读 stdin 卡住
# 关键：不让它真启动 daemon 线程；改写 threading.Thread.start 让所有线程 noop
original_start = threading.Thread.start
def fake_start(self, *args, **kwargs):
    print(f'  [daemon-skip] thread={self.name} target={getattr(self._target, "__name__", "?") if self._target else "?"}')
    return None
threading.Thread.start = fake_start

print('=' * 60)
print('main() dry-run（拦截 Thread.start 不真起线程）')
print('=' * 60)

# 替换 HTTPServer.serve_forever 为 noop
import 联网_service as main
def fake_serve(self, *args, **kwargs):
    print('  [serve-skip] serve_forever() called -> stop')
    return
# 拦截 _web_api_mod._Handler 类的实例化 — 我们不想真启 server
# 改写 main 函数的 HTTP_SERVER 赋值行为
src = '''
import sys as __sys
import traceback as __tb
import 联网_service as __main

# 替换 ThreadingHTTPServer 以避免真启 bind
class FakeHTTP:
    def __init__(self, addr, handler_cls):
        print(f"  [fake-http] bind addr={addr}, handler={handler_cls.__name__}")
        self.addr = addr
        self.handler_cls = handler_cls
    def serve_forever(self):
        print("  [fake-http] serve_forever() called -> skip")
        return
    def shutdown(self):
        pass

__main.ThreadingHTTPServer = FakeHTTP

# 调用 main()
try:
    print("[step] calling __main.main()")
    rc = __main.main()
    print(f"[step] main() returned rc={rc}")
except SystemExit as __e:
    print(f"[step] main() raised SystemExit: {__e}")
except BaseException as __e:
    print(f"[step] main() raised {type(__e).__name__}: {__e}")
    __tb.print_exc()
'''
exec(compile(src, '<dryrun>', 'exec'), {'__name__': '__main__'})
