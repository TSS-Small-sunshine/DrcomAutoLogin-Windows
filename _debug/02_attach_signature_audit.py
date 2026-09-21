# -*- coding: utf-8 -*-
"""
诊断脚本 02：解耦模块 _attach() 签名对账
- main() 中调用 _X._attach(kw1=v1, kw2=v2, ...)
- 检查每个解耦模块的 _attach() 函数签名
- 列出 missing kw / unexpected kw（签名不匹配 = runtime TypeError 根因）
"""
import sys
sys.path.insert(0, '.')
import ast as _ast
import inspect

# 读 main() 中实际传入的 kwargs（从源代码提取）
main_src = open('联网_service.py', encoding='utf-8').read()
tree = _ast.parse(main_src)

# 找出所有 _X_mod._attach(...) 调用的 kwargs
calls = {}
for node in _ast.walk(tree):
    if isinstance(node, _ast.Call):
        if isinstance(node.func, _ast.Attribute) and node.func.attr == '_attach':
            if isinstance(node.func.value, _ast.Name) and node.func.value.id.startswith('_') and node.func.value.id.endswith('_mod'):
                mod_name = node.func.value.id
                kwargs = {}
                for kw in node.keywords:
                    kwargs[kw.arg] = kw.value.__class__.__name__
                calls[mod_name] = (node.lineno, kwargs)

# 加载实际解耦模块并检查 _attach() 真实签名
mod_names = {
    '_protocol_mod': 'protocol',
    '_eula_mod': 'eula',
    '_web_api_mod': 'web_api',
    '_auto_update_mod': 'auto_update',
}

print(f"{'CALL_LINE':<10} {'MODULE':<18} {'STATUS':<14} {'MISSING_KW':<40} {'UNEXPECTED_KW'}")
print('-' * 110)

all_clean = True
for mod_alias, real_name in mod_names.items():
    try:
        real_mod = __import__(real_name)
    except Exception as exc:
        print(f"{'?':<10} {mod_alias:<18} IMPORT_FAIL     {repr(exc)}")
        all_clean = False
        continue
    if not hasattr(real_mod, '_attach'):
        print(f"{'?':<10} {mod_alias:<18} NO_ATTACH_FN   ---")
        all_clean = False
        continue
    fn = real_mod._attach
    sig = inspect.signature(fn)
    fn_params = set(sig.parameters.keys())

    if mod_alias not in calls:
        print(f"{'?':<10} {mod_alias:<18} NOT_CALLED     ---")
        all_clean = False
        continue
    line, call_kwargs = calls[mod_alias]
    call_keys = set(call_kwargs.keys())

    missing = call_keys - fn_params
    unexpected = fn_params - call_keys - {'kwargs', 'args'}  # 允许 **kwargs

    status = 'OK' if not missing and not unexpected else 'MISMATCH'
    if status == 'MISMATCH':
        all_clean = False
    missing_str = ','.join(sorted(missing))[:38]
    unexpected_str = ','.join(sorted(unexpected))[:38]
    print(f"{line:<10} {mod_alias:<18} {status:<14} {missing_str:<40} {unexpected_str}")

print()
print('OVERALL:', 'ALL_CLEAN' if all_clean else 'SOME_MISMATCH_DETECTED')
