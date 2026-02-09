# auto_xserver   自动续期脚本


## 1、你需要在仓库 Settings → Secrets and variables → Actions → Secrets 里新增：

XServer GAME 多账号自动登录和续期脚本
填写示例 (XSERVER_BATCH)：

Plaintext
# 账号1：使用全局默认TG通知
xm123456,mypassword1,210.131.111.222

# 账号2：使用该账号专属的TG通知
xm987654,mypassword2,210.131.333.444,123456:AbcDefToken,987654321

# 账号3：不发通知（如果全局也没配的话）
xm555666,mypassword3,111.222.33.44
"""

## 2、开放自动写time.txt的文件权限。
### 这个主要是用于规避github 默认60天的仓库代码没有任何变动就会自动给你发邮件并且自动禁用action的定时任务。

GITHUB_TOKEN 不用你手动创建 —— 只要你的 workflow 在 GitHub Actions 里跑起来，GitHub 会自动为每次运行生成一个临时的 secrets.GITHUB_TOKEN，在 workflow 里直接用就行（你已经在用 ${{ secrets.GITHUB_TOKEN }} 了）。

### 你需要做的通常是 给它权限、以及确认 checkout 会把它用在 push 上。

1) 你不需要创建：它默认就存在,在 workflow 里这样写就能用：
```
env:
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

```

这个 secrets.GITHUB_TOKEN 是 GitHub 自动注入的，不会出现在 “Secrets” 列表里让你手动建。


2) 你需要设置的地方：给它写权限

到仓库：

### Settings → Actions → General → Workflow permissions

选择：

✅ Read and write permissions

（如果是 “Read repository contents permission” 只读，那 git push 会失败。）

你 workflow 里这段也要保留（你已经写对了）：

```
permissions:
  contents: write
```

## 3、修改定时任务执行时间。
### main.yml里面修改你的定时任务的执行时间（建议根据你自己下面的到期时间去调整定时。）

<img width="578" height="107" alt="CleanShot 2026-02-08 at 13 17 35" src="https://github.com/user-attachments/assets/ad17bcef-cb4a-4c4b-bddd-bc97933badcf" />

