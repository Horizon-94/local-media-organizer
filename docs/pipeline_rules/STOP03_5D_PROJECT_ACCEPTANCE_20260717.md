# Stop03-5D 当前测试项目验收记录

状态：`PROJECT_ACCEPTANCE_PASS / NON_NORMATIVE`

本文件只记录当前测试素材库的实际运行结果，不是通用规则，不得把这里的数量或 run ID
写入完成判断。

## 当前来源

```text
embedding run = stop03_5d_db064187a0a53b7f8cd7c598
source 5B run = stop03_5b_8ae4389ecd58010eeb9315c1
source 5C run = stop03_5c_9f834ea8a6ed014f00ebc8f5
```

## 当前数量

```text
direct evidence = 390
direct PASS = 389
direct REVIEW excluded = 1
propagation rows = 623
frame-level documents = 758
unique text vectors = 407
reused documents = 351
document-vector links = 758
```

## 当前三路执行

```text
workers requested = 3
workers effective = 3
worker completed = 145 / 119 / 143
success = 407
pending = 0
running = 0
failed = 0
attempt_count > 1 = 0
progress.jsonl rows = 407
```

非平均分布证明本次采用动态领取，不是静态分片。

## 当前质量反查

```text
vector dimension = 1024
vector dtype = float32
invalid BLOB size = 0
invalid BLOB SHA256 = 0
non-finite vectors = 0
norm min = 0.9999999504
norm max = 1.0000000893
execution_key duplicates = 0
SQLite integrity_check = ok
foreign key errors = 0
original video read = false
network used = false
download used = false
search index created = false
```

此报告可以随当前项目归档，但不得作为其他素材库的数量门槛。
