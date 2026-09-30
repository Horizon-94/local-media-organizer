# 最终 Calibration 审计与实验性调度建议

状态：**PASS_WITH_BACKLOG**。冷启动 benchmark 语义与资源矩阵已验证；生产默认预算、长期安全上限和全流程最优组合仍为 **NOT PROVEN**。

## PROVEN：本轮实际完成

- 完整 Campaign：43 planned、39 executed semantic PASS、0失败、2 NOT_SUPPORTED、2 SKIPPED。
- 审计另发现两组 Whisper 组合只有进程重叠、没有模型共存证据。随后在独立 fresh fixture/output 中补测；输入指纹一致，正式 backend 或连续正式结果提交证明模型共存，两组均通过。
- 最终逻辑矩阵：39 VALID、2 NOT_SUPPORTED、2 SKIPPED，无 INVALID/SUSPICIOUS 遗留项。旧组合记录与原始审计保留，不抹掉历史问题。
- 完整 Campaign 与两组补测退出码均为0；所有已执行 case 的 swap增量均为0，未观察到 Metal timeout、OOM或worker crash。
- FFmpeg每档都处理同一组10视频，生成239帧；实际并行进程峰值分别为1/2/3/4/5。原始视频只读，没有复制或转码原始素材来伪造性能。
- Whisper各档固定3个视频输入，其中2个实际语音输入、1个无语音输入；x2有真实MLX转写调用重叠，不能把并行VAD当作并行Whisper。
- YOLOE/OpenCLIP各档固定30个派生视觉输入；Qwen固定5个有效派生JPEG候选；OCR/Embedding/Person有真实非空输出。所有同构档的稳定输入指纹一致。

完整数值见 [资源矩阵](2026-09-30-resource-matrix.md)、[CSV](2026-09-30-validity-results.csv)、[JSON](2026-09-30-validity-results.json)。公开报告不包含私人路径、原始文件名、转写文本、人物名或数据库内容。

## 历史数据如何处理

- 各模型原来的真实x1仍可作为短时资源参考；有原始采样的P95可重新计算，不必仅因算法修正重跑。
- 旧FFmpeg x2～x5只有 `limit-new=1`，扩容结论 INVALID；旧x1只代表单视频，不能与新10视频批次比较。
- 旧Whisper resident只是新的冷进程，常驻结论 INVALID；Qwen与Whisper多批次/idle常驻合同均未支持，明确排除。
- 旧Whisper x2只有一个真实语音输入的测试，模型扩容结论 INVALID。
- 没有同步aggregate monitor的历史组合，不能用于组合资源预算。
- 新Campaign中未真正发生重模型共存的两组组合，仅作为自然冷启动/音频准备重叠记录保留；有效共存矩阵采用后续受控启动补测。

## 固定样本下的扩容曲线

这些是本次冷启动批次的观测，不是长期homogeneous ceiling。没有三次重复/乱序控制，因此微小差异不构成显著收益证据。

| workload | worker档与真实工作秒 | 本样本结论 |
|---|---|---|
| Qwen | 1:25.56；2:23.54；3:24.81 | x2仅约8.6%吞吐收益，峰值RSS由3.36升至6.66 GiB；x3更慢且约9.94 GiB。资源平衡候选x1，不推荐凭此推广x3 |
| Whisper音频正式链 | 1:232.47；2:182.56 | x2吞吐约提升27.3%；这是完整音频工作负载，不是纯转写kernel计时 |
| YOLOE | 1:10.65；2:15.96；3:15.97 | x2/x3无收益且吞吐下降；本冷批次候选x1 |
| OpenCLIP | 1:17.08；2:17.05；3:13.82 | x2几乎持平；x3约23.6%吞吐收益，需重复确认 |
| OCR | 1:30.77；2:21.24 | x2约44.9%吞吐收益，峰值RSS由1.87升至3.54 GiB |
| Embedding | 1:10.75；2:9.70 | x2仅约10.8%吞吐收益；加载峰值漏采风险必须单独处理 |
| FFmpeg | 1:265.87；2:180.14；3:182.17；4:176.63；5:179.26 | x2取得主要收益；x4是单次最快，x5没有进一步收益。最长视频占约70.3%帧，不能据此推断机器的普遍并发上限 |

进程cold不等于文件缓存cold；未清空系统缓存。FFmpeg暖缓存x1复核约264.31秒，接近首次x1，支持其主要加速不是单纯缓存差异，但不消除所有顺序/热状态混杂。

## 两个实测候选 Profile

| 候选 | 实测组合 | 工作秒 | aggregate RSS平均/P95/峰值 GiB | CPU平均/P95/峰值% | pressure free最小% | swap增量 |
|---|---|---:|---|---|---:|---:|
| SAFE_OBSERVED_COLD：轻视觉 | YOLOE1 + OpenCLIP1 | 17.13 | 0.985/1.911/1.911 | 134.45/217.9/217.9 | 74 | 0 |
| MAX_THROUGHPUT_CANDIDATE：本样本三类混合 | Qwen1 + Whisper1 + FFmpeg1 | 287.26 | 2.809/6.601/6.737 | 376.40/616.0/890.1 | 45 | 0 |

轻视觉组合相对本次单独运行耗时之和参考约减少38.2%；三类混合相对25.56+232.47+265.87秒参考约减少45.2%。**不是**Legacy vs V2整条流水线A/B，也不是全局最优证明；不同模型的单位不能相加成一个总units/sec。

强制Qwen/Whisper真实共存补测为252.92秒，aggregate RSS峰值5.557 GiB；单独耗时之和约258.03秒，收益很小。旧自然冷启动重叠为242.50秒、3.739 GiB，但没有重模型共存。说明CPU音频准备与视觉任务的阶段重叠可能比强行让重量模型同时推理更合算。

生产SAFE profile：**NOT PROVEN**。生产MAX_THROUGHPUT profile：**NOT PROVEN**。以上仅为已测冷工作负载的候选，不能外推为常驻或任意混合档。

## Scheduler 可用的实测输入，不是最终reservation

| workload | sampled RSS P95/peak GiB | CPU P95逻辑核心约数 | backend/IO角色 | 未测边界 |
|---|---:|---:|---|---|
| Qwen | 3.362/3.362 | 0.982 | MLX/Metal，派生图像 | MLX allocator peak为4.320 GiB，不能把RSS当统一内存上界 |
| Whisper正式音频链 | 2.132/2.716 | 1.585 | MLX/Metal + 视频音频准备 + 后处理 | native persistent不支持；本次MLX allocator快照N/A |
| YOLOE | 1.218/1.218 | 1.066 | MPS，派生图像 | 长期并发上界未测 |
| OpenCLIP | 1.295/1.295 | 1.049 | MPS，派生图像 | 长期并发上界未测 |
| OCR | 1.867/1.867 | 1.104 | 正式OCR链，派生图像 | 实际CPU/内存cost随画面尺寸和文字复杂度变化 |
| Embedding | 1.225/1.225 | 4.135 | 正式MPS text embedding | 正式worker lifetime peak约3.694 GiB；采样峰值不是加载上界 |
| FFmpeg | 1.185/1.186 | 5.440 | VideoToolbox优先/软件fallback，原视频读取 | 只测内置SSD；工作长度明显倾斜 |
| Person/ReID | 1.034/1.034 | 0.969 | 正式InsightFace链，派生图像 | 三个有效人脸输入；约2.18秒监测窗口，短峰值可能漏采 |

Qwen x1 allocator末尾快照：active=3,472,929,392 bytes、cache=0、peak=4,638,622,365 bytes。它们不是独立显存，也不能与RSS相加成实测总内存。

建议的**实验性 admission合同**：

`ready + dependency satisfied + tested workload mix fits + measured startup/steady-state headroom available + system safety healthy + bounded IO + single DB writer = admit`

1. 保留有限队列、背压、单DB writer、lease/retry/idempotency与Legacy退路；不回到全阶段barrier。
2. 仅使用确实测过的并发/组合区域；不能从单模型x2推导出未测的所有模型x2组合。
3. 分离音频decode/VAD准备与重量backend admission，避免因进程已启动就认定重模型已占用或已经共存。
4. 加载期reservation、常驻idle cost、系统安全reserve、compressed/swap阈值的生产数字仍为N/A；不凭经验写入正式配置。
5. 发生swap持续上涨、实际pressure危险、Metal错误或worker crash时停止新增重量任务，等待已有任务结束后降档；不激进自动扩容。
6. 选择使剩余关键路径更短的任务，不以CPU/内存占满为目标。低收益扩容与强制共存不应自动获得优先级。

## 停止位置与下一阶段

本轮不修改生产Scheduler默认值，不修改安装App。可以进入基于这些真实数据的实验性Scheduler调参；不能宣布生产预算完成或默认切换V2。

下一阶段需要：持续event-driven mixed soak、加载期峰值/常驻生命周期验证、候选配置重复与启动顺序控制、共享DB writer验收。HDD没有真实绑定，保持SKIPPED；当前HDD/soak case不是已执行功能验收。
