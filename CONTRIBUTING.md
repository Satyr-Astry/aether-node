# 贡献指南

## 开发环境

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install ruff mypy
```

## 提交前必须通过的检查

```powershell
.venv\Scripts\python.exe -m ruff check aether
.venv\Scripts\python.exe -m mypy aether --ignore-missing-imports
Get-ChildItem tests/test_*.py | ForEach-Object { .venv\Scripts\python.exe $_.FullName }
```

三者都必须是零失败。CI 跑的是同样的命令，本地过了 CI 基本就过。

## 测试的写法约定

测试是**独立可执行脚本**，不是 pytest 用例，靠退出码报告结果：

```python
fails = []

def check(name, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        fails.append(name)

# ... 若干断言 ...

if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all xxx checks passed")
```

这么设计是因为每个套件要能单独跑、单独看输出，CI 里也是逐个执行的。
**不要改成 pytest 风格**，那会让 `python tests/test_xxx.py` 直接用不了。

端到端脚本用 PowerShell（`tests/e2e_*.ps1`），因为它们要起进程、杀进程、探端口。

## 用假数据，不要连真实网络

- 外部 API 测试用 `tests/mock_openai.py` 起的假端点
- 上链测试用离线向量，不要往测试网发交易
- 网络行为测试在本机用不同端口 + 非回环 IP 模拟

## 提交信息

语义化前缀：`feat:` / `fix:` / `test:` / `docs:` / `chore:` / `build:` / `ci:`。
正文说清「为什么改」而不只是「改了什么」，尤其是修 bug 时要说清根因。

## 一个提醒

改完代码记得同步 README 的「已实现」与「明确未实现」两节。这个项目最怕的是
**代码在增长而验证没跟上**——把没测过的东西写成已验证，比不写更糟。
