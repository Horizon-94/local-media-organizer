import Combine
import AppKit
import AVFoundation
import AVKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

// Display-only conversion: original evidence, paths and identifiers remain unchanged.
private func simplifiedContent(_ text: String) -> String {
    text.applyingTransform(StringTransform("Traditional-Simplified"), reverse: false) ?? text
}
private struct ContentText: View {
    @EnvironmentObject var model: ArchiveModel
    let value: String
    init(_ value: String) { self.value = value }
    var body: some View { Text(model.showOriginalContent ? value : simplifiedContent(value)) }
}
private struct ContentLanguageControl: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View {
        Toggle("查看原文（关闭后显示简体）", isOn: $model.showOriginalContent)
            .toggleStyle(.checkbox).font(.caption)
            .help("仅转换显示字形；原始转写、检索证据与时间码保持不变。")
    }
}

private final class VideoPreviewWindowController: NSWindowController {
    var readinessObservation: NSKeyValueObservation?
    var player: AVPlayer?
    private var closeObserver: NSObjectProtocol?

    override init(window: NSWindow?) {
        super.init(window: window)
        if let window {
            closeObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification,
                object: window,
                queue: .main
            ) { [weak self] _ in
                self?.stopPlayback()
            }
        }
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
    }

    private func stopPlayback() {
        readinessObservation?.invalidate()
        readinessObservation = nil
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        player = nil
    }

    deinit {
        stopPlayback()
        if let closeObserver {
            NotificationCenter.default.removeObserver(closeObserver)
        }
    }
}

private let archiveBlue = Color(red: 0.086, green: 0.416, blue: 0.941)
private let archiveBackground = Color(nsColor: .underPageBackgroundColor)
private let archiveMuted = Color(red: 0.40, green: 0.44, blue: 0.52)
private let archiveGreen = Color(red: 0.08, green: 0.62, blue: 0.28)
private let archiveOrange = Color(red: 0.95, green: 0.55, blue: 0.08)
private var bundledAppVersion: String {
    Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        ?? "开发版"
}
private var bundledAppName: String {
    Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "本地数据库"
}
private var bundledBuildDate: String {
    Bundle.main.object(forInfoDictionaryKey: "HorizonBuildDate") as? String ?? "开发构建"
}

private func formatBytes(_ value: Int64) -> String {
    let formatter = ByteCountFormatter()
    formatter.allowedUnits = [.useBytes, .useKB, .useMB, .useGB, .useTB]
    formatter.countStyle = .file
    return formatter.string(fromByteCount: value)
}

private func formatMemoryGB(_ value: Int64) -> String {
    String(format: "%.2f GB", Double(value) / 1_073_741_824)
}

private func formatSeconds(_ value: Double?) -> String {
    guard let value else { return "--" }
    let total = max(0, Int(value.rounded()))
    let hours = total / 3600
    let minutes = (total % 3600) / 60
    let seconds = total % 60
    if hours > 0 { return "\(hours)小时\(minutes)分\(seconds)秒" }
    if minutes > 0 { return "\(minutes)分\(seconds)秒" }
    return "\(seconds)秒"
}

private func etaDisplay(_ eta: Double?, status: String?) -> String {
    let normalized = (status ?? "").uppercased()
    if normalized == "SUCCESS" || normalized == "COMPLETED" { return "已完成" }
    if normalized == "FAILED" || normalized == "CANCELLED" || normalized == "RECOVERABLE" || normalized == "FAILED_OUTPUT_ACCEPTANCE" {
        return "暂无预计时间"
    }
    if let eta { return "预计剩余约 \(formatSeconds(eta))" }
    return "当前阶段预计剩余：正在估算…"
}

private func progressStatusDisplay(_ status: String?, active: Bool) -> String {
    let normalized = (status ?? "").uppercased()
    if normalized == "SUCCESS" || normalized == "COMPLETED" { return "已完成" }
    if normalized == "FAILED" || normalized == "FAILED_OUTPUT_ACCEPTANCE" { return "失败" }
    if normalized == "CANCELLED" { return "已停止，可从断点继续" }
    if normalized == "RECOVERABLE" { return "已保存进度，可从断点继续" }
    if normalized == "STOPPING" { return "正在安全停止并保存进度" }
    if active { return "处理中" }
    return "等待开始"
}

private func stageStatusLabel(_ status: String) -> String {
    switch status {
    case "success": return "已完成"
    case "running": return "进行中"
    case "waiting_dependency": return "等待依赖"
    case "waiting_scheduler": return "等待调度"
    case "not_triggered": return "尚未触发"
    case "retryable": return "等待重试"
    case "failed": return "失败"
    case "stopping": return "正在安全停止"
    case "drained_open": return "当前队列已处理完"
    default: return "待处理"
    }
}
private func stageStatusColor(_ status: String) -> Color {
    switch status {
    case "success": return archiveGreen
    case "failed": return .red
    case "running": return archiveBlue
    default: return archiveOrange
    }
}
private func stageStatusIcon(_ status: String) -> String {
    switch status {
    case "success": return "checkmark.circle.fill"
    case "running": return "circle.dotted"
    case "failed": return "xmark.circle.fill"
    case "retryable": return "arrow.clockwise.circle"
    case "stopping": return "pause.circle"
    case "drained_open": return "checkmark.circle"
    case "waiting_dependency": return "arrow.down.circle"
    case "not_triggered": return "circle"
    default: return "clock.fill"
    }
}

struct SourceStats: Decodable { let count: Int; let bytes: Int64 }
struct StorageStats: Decodable { let total: Int64; let used: Int64; let free: Int64 }
struct Recognition: Decodable {
    let openclipVisualUnits: Int
    let yoloeDetectedVisualUnits: Int
    let qwenSuccess: Int
    let ocrCompleted: Int
    let textVectors: Int
}
struct Overview: Decodable {
    let source: [String: SourceStats]
    let sourceTotalCount: Int
    let sourceTotalBytes: Int64
    let visualUnitTotalCount: Int
    let recognition: Recognition
    let duplicateGroupCount: Int
    let timelapseGroupCount: Int
    let processingErrorCount: Int?
    let latestPipelineActivity: String?
    let storage: StorageStats?
}
struct PipelineStage: Decodable, Identifiable {
    let key: String; let name: String; let status: String
    let done: Int; let total: Int; let percent: Double; let description: String
    let errorSummary: String?; let logPath: String?
    let currentItem: String?; let successCount: Int?; let skippedCount: Int?
    let failedCount: Int?; let etaSeconds: Double?; let etaBasis: String?
    let configuredWorkers: Int?; let actualWorkers: Int?; let ffmpegProcesses: Int?
    let modelWorkers: Int?; let bytesProcessed: Int64?; let outputFiles: Int?
    let stageOutputBytes: Int64?; let databaseDeltaBytes: Int64?
    let startedWorkers: Int?; let aliveWorkers: Int?; let activeWorkers: Int?
    let idleWorkers: Int?; let crashedWorkers: Int?; let restartCount: Int?
    let queuePending: Int?; let queueRunning: Int?
    let retryableCount: Int?; let failedFinalCount: Int?; let stateReason: String?
    let reportPaths: [String: String]?
    var id: String { key }
}
struct ProductLifecycleState: Decodable {
    let currentPass: String
    let retryableCount: Int
    let failedFinalCount: Int
    let failedSourceList: [String]
    let errorSummary: [String: String]
}
struct PipelineState: Decodable {
    let runtimeDashboard: RuntimeDashboard?
    let lifecycle: ProductLifecycleState?
    let stages: [PipelineStage]
    let schedulerMode: String?
    let currentJobsTotal: Int?
    let currentJobsDone: Int?
    let currentJobsRunning: Int?
    let currentJobsPending: Int?
    let currentJobsRetryable: Int?
    let currentJobsFailedFinal: Int?
    let overallPercent: Double
    let searchReady: Bool
    let failedRecordCount: Int?
    let fullPipelineLauncherStatus: String?
    let overallEtaSeconds: Double?
    let overallEtaBasis: String?
    let failedStageKey: String?
    let failedStageName: String?
    let errorSummary: String?
    let errorDetails: String?
    let errorLogPath: String?
}
struct RuntimeTimer: Decodable {
    let taskStart: String?; let taskEnd: String?; let totalElapsed: Double
    let activeProcessing: Double?; let pausedTime: Double?; let paused: Bool
    let basis: String; let benchmarkElapsed: Double?
}
struct RuntimeStageSummary: Decodable, Identifiable {
    let stage: String; let name: String; let done: Int; let running: Int
    let pending: Int; let failed: Int; let runnable: Int; let blocker: String
    let sourceCount: Int; let currentSources: [String]
    var id: String { stage }
}
struct RuntimeLane: Decodable, Identifiable {
    let kind: String; let name: String; let sourceTotal: Int; let sourceDone: Int
    let failed: Int; let eligible: Int; let covered: Int; let skipped: Int
    let producerDone: Int; let ready: Int; let frames: Int; let currentItem: String
    let waiting: Int; let blocker: String; let stages: [RuntimeStageSummary]
    var id: String { kind }
}
struct RuntimeSource: Decodable, Identifiable {
    let sourceId: String; let filename: String; let mediaKind: String
    let state: String; let reason: String
    var id: String { sourceId }
}
struct RuntimeDashboard: Decodable {
    let lanes: [RuntimeLane]; let timer: RuntimeTimer
    let stageSummaries: [RuntimeStageSummary]; let sources: [RuntimeSource]
    let sourceTotal: Int; let sourceDone: Int; let mainBlocker: String; let gpuMetric: String
    let visualCoverageStatus: String?
    let controlSummary: String?
    let highValueSummary: String?
}
struct RuntimeSourceStep: Decodable, Identifiable {
    let key: String; let name: String; let state: String; let done: Int
    let total: Int; let reason: String
    var id: String { key }
}
struct RuntimeSourceDetail: Decodable {
    let status: String; let sourceId: String; let filename: String; let lifecycle: String
    let activeRevision: String?; let steps: [RuntimeSourceStep]; let firstBlockingReason: String
    let candidateReasonLines: [String]?
}
struct DatabaseState: Decodable { let integrityCheck: String; let foreignKeyErrorCount: Int?; let checkedAt: Double? }
struct DatabasePreflight: Decodable {
    let databasePath: String?
    let databaseError: String?
}
struct RuntimeState: Decodable {
    let searchMode: String?
    let ready: Bool; let checks: [String: Bool]
    let uncoveredVideoSourceCount: Int?
    let databasePreflight: DatabasePreflight?
}
struct RuntimeModelItem: Decodable, Identifiable {
    let key: String; let path: String; let ready: Bool
    var id: String { key }
}
struct RuntimeContractState: Decodable {
    let ready: Bool; let modelRoot: String?
    let missing: [String]; let errors: [String]
    let modelItems: [RuntimeModelItem]?
}
struct HardwareRecommendation: Decodable {
    let modelWorkers: Int; let ocrWorkers: Int; let embeddingWorkers: Int
    let frameExtractWorkers: Int; let ioWorkers: Int
    let estimatedMaxModelWorkers: Int; let estimatedMaxOcrWorkers: Int
}
struct HardwareState: Decodable {
    let chip: String; let machineFamily: String; let cpuCoresTotal: Int
    let cpuPerformanceCores: Int?; let cpuEfficiencyCores: Int?
    let gpuName: String; let gpuCores: Int?; let unifiedMemoryGb: Double?
    let recommendation: HardwareRecommendation
}
struct LiveResources: Decodable {
    let systemMemoryTotalBytes: Int64?; let systemMemoryUsedBytes: Int64?
    let workers: [RuntimeWorker]?
    let activePid: Int?; let processAlive: Bool; let cpuPercent: Double
    let memoryBytes: Int64; let processCount: Int?; let swapUsedBytes: Int64?; let processState: String?
    let sampleError: String?; let sourceScanned: Bool
}
struct RuntimeWorker: Decodable, Identifiable {
    let pid: Int; let purpose: String; let cpuPercent: Double; let memoryBytes: Int64
    var id: Int { pid }
}
struct RecentRun: Decodable, Identifiable {
    let runId: String?; let stage: String?; let status: String?
    let inputCount: Int?; let outputCount: Int?; let startedAt: String?
    let finishedAt: String?; let errorMessage: String?
    var id: String { (runId ?? "run") + (startedAt ?? "") }
}
struct ExistingLibrary: Decodable, Identifiable {
    let taskId: String; let taskName: String; let taskPath: String
    let database: String; let sourceRoot: String; let createdAt: String
    let status: String; let imageCount: Int; let videoCount: Int
    let elapsedSeconds: Double?; let elapsedHuman: String?
    let isActive: Bool
    var id: String { taskPath }
    var isValidationLibrary: Bool {
        let parts = URL(fileURLWithPath: taskPath).pathComponents
        return parts.contains("STREAMING_PIPELINE_GATE_REDUCTION") ||
            parts.contains(where: { $0.hasPrefix("BUILD152_SELF_CHECK_") }) ||
            ["PROJECT_A_LIFECYCLE_V2", "PROJECT_B_LIFECYCLE_V2", "WHISPER_CONTROLLED_BACKLOG", "WHISPER_CONTROLLED_BACKLOG_V2", "WHISPER_NORMAL_FINAL", "BUILD151_AUDIO_CHAIN_TEST"].contains(taskName)
    }
    var displayName: String {
        "\(taskName)｜图片 \(imageCount)｜视频 \(videoCount)｜\(createdAt)"
    }
}
struct ActiveRun: Decodable, Identifiable {
    let runId: String?; let stage: String?; let total: Int?; let completed: Int?
    let pending: Int?; let elapsedSeconds: Double?; let remaining: Int?
    let percent: Double?; let etaSeconds: Double?
    var id: String { runId ?? UUID().uuidString }
}
struct DuplicateMember: Decodable, Identifiable {
    let sourceFileId: String; let sourceContentId: String?; let fileName: String
    let relativePath: String; let absolutePath: String; let folderPath: String
    let sizeBytes: Int64; let identityStatus: String; let isCanonical: Bool
    var id: String { sourceFileId }
}
struct DuplicateItem: Decodable, Identifiable {
    let duplicateGroupId: String; let memberCount: Int?; let totalBytes: Int64?
    let canonicalReason: String?; let fileName: String?; let relativePath: String?
    let members: [DuplicateMember]
    var id: String { duplicateGroupId }
}
struct DuplicatePayload: Decodable { let total: Int; let items: [DuplicateItem] }
struct TimelapseFrame: Decodable, Identifiable {
    let visualUnitId: String?; let representativePosition: String?
    let sourceRelativePath: String?; let derivedId: String?; let timePositionMs: Int?
    let previewPath: String?; let sourcePath: String?
    var id: String {
        let visual = (visualUnitId ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !visual.isEmpty { return visual }
        return "\(sourceRelativePath ?? "")|\(representativePosition ?? "representative")"
    }
}
struct TimelapseGroup: Decodable, Identifiable {
    let sequenceId: String; let keyframeCount: Int?; let firstPath: String?
    let createdAt: String?; let sourcePhotoCount: Int?; let sourceRelativeDir: String?
    let sourceFolder: String?; let frames: [TimelapseFrame]
    var id: String { sequenceId }
}
struct TimelapsePayload: Decodable { let total: Int; let items: [TimelapseGroup] }
struct SavedScheduler: Decodable {
    let mode: String; let modelWorkers: Int; let frameExtractWorkers: Int
}
struct SavedVideoSampling: Decodable { let frameIntervalSeconds: Double }
struct SavedHighValuePolicy: Decodable { let mode: String; let imageScope: String }
struct YoloeKeywordEntry: Decodable {
    let label: String; let zh: String; let categoryZh: String
    var editableLine: String { label == zh ? label : "\(label) = \(zh)" }
}
struct YoloeKeywordProfile: Decodable {
    let enableBExtended: Bool
    let aCore: [YoloeKeywordEntry]
    let bExtended: [YoloeKeywordEntry]
}
struct SavedProcessingProfile: Decodable {
    let profileId: String
    let scheduler: SavedScheduler
    let videoSampling: SavedVideoSampling
    let highValuePolicy: SavedHighValuePolicy
    let yoloeKeywords: YoloeKeywordProfile?
}
struct Snapshot: Decodable {
    let status: String; let version: String; let overview: Overview
    let configurationState: String
    let pipeline: PipelineState; let database: DatabaseState; let searchRuntime: RuntimeState
    let runtimeContract: RuntimeContractState
    let hardware: HardwareState; let resources: LiveResources
    let recentRuns: [RecentRun]; let existingLibraries: [ExistingLibrary]; let activeRuns: [ActiveRun]
    let duplicateGroups: DuplicatePayload; let timelapseGroups: TimelapsePayload
    let savedProfilePath: String; let hasSavedProfile: Bool
    let savedProfile: SavedProcessingProfile?
    let yoloeKeywordProfile: YoloeKeywordProfile
    let yoloeDefaultKeywordProfile: YoloeKeywordProfile
    let databaseReadError: String?
}
struct TaskDetailResponse: Decodable {
    let status: String; let taskId: String; let taskName: String; let taskPath: String
    let sourceRoot: String; let createdAt: String; let taskStatus: String
    let startedAt: String?; let finishedAt: String?; let pipeline: PipelineState
    let elapsedSeconds: Double?; let elapsedHuman: String?
    let indexStorage: TaskIndexStorage?
    let error: String?
}
struct TaskIndexStorage: Decodable {
    let totalBytes: Int64; let totalFileCount: Int
    let status: String; let sourceRootScanned: Bool
}
struct StorageCategory: Decodable {
    let bytes: Int64; let fileCount: Int
    let safeToRemoveCount: Int; let affectsResumeCount: Int
}
struct StorageAuditResponse: Decodable {
    let status: String; let taskId: String; let taskName: String
    let totalBytes: Int64; let totalFileCount: Int
    let categories: [String: StorageCategory]
    let readOnly: Bool; let deletionPerformed: Bool; let policy: String
}
struct StorageCleanupItem: Decodable, Identifiable {
    let path: String; let relativePath: String; let bytes: Int64
    let category: String; let affectsResume: Bool; let reason: String
    var id: String { path }
}
struct StorageCleanupPlan: Decodable {
    let status: String; let planId: String; let taskId: String; let taskPath: String
    let candidateCount: Int; let candidateBytes: Int64
    let excludedResumeAffectingCount: Int; let items: [StorageCleanupItem]
    let confirmationPhrase: String; let readOnly: Bool; let policy: String
}
struct StorageCleanupResult: Decodable {
    let status: String; let planId: String; let removedCount: Int
    let removedBytes: Int64; let deletionPerformed: Bool
    let originalMediaTouched: Bool; let taskDatabaseTouched: Bool
}
struct StorageDifference: Decodable {
    let fileCountDeltaRightMinusLeft: Int
    let bytesDeltaRightMinusLeft: Int64
}
struct TaskComparisonResponse: Decodable {
    let status: String; let categoryDifference: [String: StorageDifference]
    let interpretation: String; let readOnly: Bool; let deletionPerformed: Bool
}
struct SearchCoverage: Decodable {
    let eligibleVisualUnitCount: Int
    let scannedVisualVectorCount: Int
    let scannedTextVectorCount: Int
}
struct SearchProgressEvent: Decodable {
    let contract: String; let stage: String; let stageIndex: Int; let totalStages: Int
    let message: String; let detail: String?
    let completed: Int?; let total: Int?; let elapsedSeconds: Double?
    var searchState: String? = nil; var scannedCount: Int? = nil; var totalCount: Int? = nil
    var currentSourceResultCount: Int? = nil; var generation: Int? = nil; var updateMode: String? = nil
    var ordering: String? = nil; var resultItems: [SearchResult]? = nil
    var resultOffset: Int? = nil; var resultLimit: Int? = nil; var nextResultOffset: Int? = nil
}
struct PersonClusterLink: Decodable, Identifiable {
    let personClusterId: String; let memberCount: Int
    let distinctSourceCount: Int; let clusterConfidence: String
    let humanReviewStatus: String; let displayName: String
    let isLocalIdentity: Bool?; let manualAssignment: Bool?
    var id: String { personClusterId }
}
struct PersonClusterSummary: Decodable, Identifiable {
    let personClusterId: String; let displayName: String
    let memberCount: Int; let distinctSourceCount: Int
    let clusterConfidence: String; let humanReviewStatus: String
    let previewPath: String?; let mediaType: String?; let timePositionMs: Int?
    let tags: [String]?; let mergedClusterCount: Int?; let isLocalIdentity: Bool?
    let personBbox: [Double]?
    var id: String { personClusterId }
}
struct PersonClusterCatalogResponse: Decodable {
    let status: String; let total: Int; let items: [PersonClusterSummary]
    let capabilityNote: String?; let error: String?
}
// A private pipe-owned session, not a network service. One request at a time.
final class Build151SearchSessionTransport {
    private let helperScript:String
    init(helperScript:String="build151_search_session.py") { self.helperScript=helperScript }
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var pending=Data()
    private var scannedBytes=0
    private let serial=NSLock()
    func query(contents:URL,config:URL,environment:[String:String],arguments:[String],queryId:String,operation:String="search",deadlineMono:Double?=nil,isAllowed:@escaping()->Bool={true},
               started:@escaping(Process)->Void,progress:@escaping(Data)->Void) throws -> Data {
        serial.lock();defer {serial.unlock()}
        guard isAllowed() else {throw NSError(domain:"SearchSessionCancelledBeforeStart",code:1)}
        if let deadlineMono, deadlineMono <= ProcessInfo.processInfo.systemUptime {
            return try JSONSerialization.data(withJSONObject:["status":"INCOMPLETE","error":"TimeoutError","error_reason":"search_deadline_45_seconds","failed_component":"query","recoverable":true])
        }
        build151LifecycleEvent("query_transport_start id=\(queryId)")
        defer {build151LifecycleEvent("query_transport_end id=\(queryId)")}
        if process?.isRunning != true {
            let p=Process();let stdinPipe=Pipe();let stdoutPipe=Pipe();let stderrPipe=Pipe()
            p.executableURL=contents.appendingPathComponent("Helpers/PipelinePython/system")
            p.arguments=[contents.appendingPathComponent("Resources/"+helperScript).path,"--config",config.path,"--owner-pid",String(ProcessInfo.processInfo.processIdentifier),"--initial-query-id",queryId]
            p.environment=environment;p.standardInput=stdinPipe;p.standardOutput=stdoutPipe;p.standardError=stderrPipe
            // Always drain diagnostics so a child cannot stall on a full pipe.
            stderrPipe.fileHandleForReading.readabilityHandler={ handle in
                let data=handle.availableData
                if data.isEmpty {handle.readabilityHandler=nil}
            }
            p.terminationHandler={ child in Build151ChildOwner.finish(child) }
            try p.run();Build151ChildOwner.register(p)
            process=p;input=stdinPipe.fileHandleForWriting;output=stdoutPipe.fileHandleForReading;pending=Data();scannedBytes=0
        }
        guard let p=process,let input=input,let output=output else {throw NSError(domain:"SearchSession",code:1)}
        var successful=false
        defer { if !successful && p.isRunning {p.terminate()} }
        started(p)
        var payload:[String:Any]=["query_id":queryId,"arguments":arguments,"operation":operation]
        if let deadlineMono {payload["deadline_mono"]=deadlineMono}
        var request=try JSONSerialization.data(withJSONObject:payload);request.append(10)
        try input.write(contentsOf:request)
        while true {
            while let newline=pending.dropFirst(scannedBytes).firstIndex(of:10) {
                let line=Data(pending.prefix(upTo:newline));pending.removeSubrange(...newline);scannedBytes=0
                guard let envelope=try JSONSerialization.jsonObject(with:line) as? [String:Any] else {throw NSError(domain:"SearchSession",code:2)}
                if envelope["kind"] as? String == "ready" {continue}
                guard envelope["query_id"] as? String == queryId else {throw NSError(domain:"SearchSessionIdentity",code:3)}
                if envelope["kind"] as? String == "progress",let event=envelope["event"] {
                    progress(try JSONSerialization.data(withJSONObject:event))
                } else if envelope["kind"] as? String == "result",let result=envelope["result"] {
                    if let value=result as? [String:Any], let metrics=value[helperScript == "build151_snapshot_session.py" ? "snapshot_session_metrics" : "query_session_metrics"], let bytes=try? JSONSerialization.data(withJSONObject:metrics,options:[.sortedKeys]), let text=String(data:bytes,encoding:.utf8) {
                        build151LifecycleEvent("\(helperScript == "build151_snapshot_session.py" ? "snapshot_session_result" : "search_session_result") transport_id=\(queryId) metrics=\(text)")
                    }
                    successful=true;return try JSONSerialization.data(withJSONObject:result)
                }
            }
            scannedBytes=pending.count
            let chunk=output.availableData
            if chunk.isEmpty {p.waitUntilExit();throw NSError(domain:"SearchSessionClosed",code:Int(p.terminationStatus),userInfo:[NSLocalizedDescriptionKey:"搜索会话已结束或取消；可重新查询"])}
            pending.append(chunk)
            if pending.count>64*1024*1024 {p.terminate();throw NSError(domain:"SearchSessionResponseLimit",code:4)}
        }
    }
}

struct SearchResult: Decodable, Identifiable {
    let libraryTaskPath: String?; let libraryName: String?
    struct ObjectLabelHit: Decodable {
        let label: String?; let labelZh: String?; let confidence: Double?
    }
    let resultId: String?; let visualUnitId: String?; let sourceRelativePath: String?; let mediaType: String?
    let sourceContentId: String?; let sourceFrameCount: Int?; let resultLevel: String?
    let timecode: String?; let previewSegmentStartTimecode: String?
    let previewSegmentEndTimecode: String?; let previewSegmentStartMs: Int?
    let hybridScore: Double?; let openclipCosine: Double?; let textSemanticScore: Double?
    let textPreview: String?; let environmentLabel: String?
    let relevanceReasons: [String]?; let textExactMatch: Bool?; let yoloeQueryMatch: Bool?
    let previewPath: String?; let sourcePath: String?
    let score: Double?; let hitReason: String?; let hitField: String?
    let sourceOnline: Bool?; let canOpenOriginal: Bool?
    let matchedObjectLabels: [ObjectLabelHit]?
    let matchedTextTerms: [String]?
    let audioTranscriptMatch: Bool?; let audioEvidenceId: String?
    let audioStartTimeMs: Int?; let audioEndTimeMs: Int?; let audioHitTimeMs: Int?
    let personClusters: [PersonClusterLink]?
    let personBbox: [Double]?
    let userAnnotation: UserAssetAnnotation?
    let sourceId: String?; let sourceType: String?; let originalPath: String?
    let displayName: String?; let representativeThumbnail: String?
    let topMatch: SearchRepresentativeFrame?; let otherMatchCount: Int?
    let matchClusters: [SearchMatchCluster]?; let otherMatchClusters: [SearchMatchCluster]?
    let matchReasons: [SearchMatchReason]?; let finalScore: Double?
    let evidenceSummary: String?
    let evidenceCategory: String?; let searchIntent: String?; let sourceRevision: String?
    let timePositionMs: Int?; let resultEndMs: Int?
    let searchResultContract: String?; let sourceMatchCount: Int?
    let sourceMatchTimePositionsMs: [Int]?
    var id: String { resultId ?? UUID().uuidString }
    var exportSelectionId: String {
        resultId ?? "\(sourceContentId ?? "unknown")|\(timecode ?? "00:00")|\(previewPath ?? "")"
    }
}
// RC2-04B1 is the sole authority for this surface.  These types deliberately
// retain provenance and references even where the normal UI only shows a
// human-readable evidence label.
struct VideoContentEvidenceRef: Decodable, Identifiable {
    let executionKey: String?
    let documentId: String?
    let artifactId: String?
    let sourceRevision: String?
    let stage: String?
    let provenance: JSONValue?
    let observationId: String?
    var id: String {
        [executionKey, documentId, artifactId, stage].compactMap { $0 }.joined(separator: "|")
    }
}
struct VideoContentObject: Decodable, Identifiable {
    let label: String
    let confidence: Double?
    var id: String { "\(label)|\(confidence ?? -1)" }
}
struct VideoContentPerson: Decodable, Identifiable {
    let personId: String
    let identityStatus: String?
    var id: String { personId }
}
struct VideoContentProvenance: Decodable {
    let sourceRevision: String?
    let evidenceTypes: [String]?
}
struct VideoContentSegment: Decodable, Identifiable {
    let startTimeMs: Int
    let endTimeMs: Int
    let title: String
    let shortDescription: String?
    let visualDescription: String?
    let transcriptText: String?
    let transcriptSummary: String?
    let objects: [VideoContentObject]
    let ocrText: String?
    let people: [VideoContentPerson]
    let evidenceRefs: [VideoContentEvidenceRef]
    let provenance: VideoContentProvenance
    let confidence: Double?
    let evidenceAvailability: String
    var id: String { "\(startTimeMs)|\(endTimeMs)|\(title)" }
}
struct VideoContentTranscriptMinute: Decodable, Identifiable {
    let startTimeMs: Int
    let endTimeMs: Int
    let text: String
    let evidenceRefs: [VideoContentEvidenceRef]
    var id: String { "\(startTimeMs)|\(endTimeMs)" }
}
struct VideoContentKeyMoment: Decodable, Identifiable {
    let startTimeMs: Int
    let endTimeMs: Int
    let reasonTypes: [String]
    let evidenceRefs: [VideoContentEvidenceRef]
    var id: String { "\(startTimeMs)|\(endTimeMs)|\(reasonTypes.joined(separator: ","))" }
}
struct VideoContentEvidenceSummary: Decodable {
    let whisperDocumentCount: Int
    let qwenDocumentCount: Int
    let ocrDocumentCount: Int
    let yoloeEvidenceCount: Int
    let openclipCurrentJobCount: Int
    let personEvidenceCount: Int
    let evidenceTotal: Int
}
struct SpokenQA: Decodable { let documentId: String; let startMs: Int; let question: String; let answer: String; let basis: String; let answerStartMs: Int?; let answerEndMs: Int? }
struct SpokenItem: Decodable, Identifiable {
    let sourceId: String; let sourceRevision: String; let displayName: String; let libraryName: String
    let startMs: Int; let endMs: Int; let contentLabel: String; let excerpt: String
    let classificationReason: String; let coverageNote: String; let qaClues: [SpokenQA]
    var id: String { sourceId + ":" + sourceRevision }
}
struct SpokenCatalogue: Decodable {
    let status: String; let contract: String; let items: [SpokenItem]; let total: Int
    let offset: Int; let limit: Int; let nextOffset: Int?; let message: String
}
struct VideoContentTimeline: Decodable {
    static let contractV1 = "media_archive_video_content_timeline_v1"
    let status: String
    let contract: String
    let sourceId: String
    let sourceRevision: String
    let fileName: String
    let relativePath: String
    let sourcePath: String
    let sourceOnline: Bool
    let sourceAvailability: String?; let canOpenOriginal: Bool?
    let videoDurationMs: Int?
    let segments: [VideoContentSegment]
    let transcriptMinutes: [VideoContentTranscriptMinute]
    let keyMoments: [VideoContentKeyMoment]
    let overview: String
    let qaClues: [String]
    let qaCluePolicy: String
    let evidenceSummary: VideoContentEvidenceSummary
    let segmentGroupingPolicy: [String: JSONValue]
    let databaseWrite: Bool
    let modelRun: Bool
}
indirect enum JSONValue: Decodable {
    case string(String), integer(Int), number(Double), boolean(Bool), array([JSONValue]), object([String: JSONValue]), null
    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .boolean(value) }
        else if let value = try? container.decode(Int.self) { self = .integer(value) }
        else if let value = try? container.decode(Double.self) { self = .number(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode([JSONValue].self) { self = .array(value) }
        else { self = .object(try container.decode([String: JSONValue].self)) }
    }
}
struct SearchMatchReason: Decodable, Identifiable {
    let type: String?; let authority: String?; let label: String?
    let confidence: Double?; let bbox: [Double]?; let score: Double?; let text: String?
    var id: String { "\(type ?? "reason")|\(label ?? text ?? "")|\(score ?? 0)" }
}
struct SearchRepresentativeFrame: Decodable {
    let resultId: String?; let visualUnitId: String?; let derivedId: String?
    let previewPath: String?; let timePositionMs: Int?; let timecode: String?
}
struct SearchMatchCluster: Decodable, Identifiable {
    let clusterId: String; let startTimeMs: Int; let endTimeMs: Int
    let representativeTimeMs: Int?; let representativeFrame: SearchRepresentativeFrame?
    let matchCount: Int; let topScore: Double?; let matchReasons: [SearchMatchReason]?
    let evidenceCategory: String?; let evidenceText: String?
    var id: String { clusterId }
}
struct UserAssetAnnotation: Decodable {
    let tags: [String]; let note: String; let favorite: Bool
    let rating: Int; let ignored: Bool; let updatedAt: Double?
}
struct SearchResponse: Decodable {
    struct LibraryError: Decodable { let library: String; let reason: String }
    let libraryErrors: [LibraryError]?
    let status: String; let elapsedSeconds: Double?; let coverage: SearchCoverage?
    let resultCount: Int?; let resultItems: [SearchResult]?; let error: String?
    let resultTotalCount: Int?; let resultOffset: Int?; let resultLimit: Int?
    let nextResultOffset: Int?; let resultCountByMedia: [String: Int]?
}
private struct SearchNavigationSnapshot {
    let returnTitle: String
    let query: String; let mediaType: String; let previewWindow: String
    let searchPathPrefix: String; let searchDateFrom: String; let searchDateTo: String
    let searchRequireOCR: Bool; let searchRequirePerson: Bool
    let searchStatus: String; let searchDiagnostic: String
    let searchResults: [SearchResult]; let bufferedSearchResults: [SearchResult]
    let searchCoverage: SearchCoverage?; let searchTotalCount: Int
    let nextSearchOffset: Int?; let serverNextSearchOffset: Int?
    let lastSearchSignature: String; let lastSearchMediaSummary: String
    let lastSuccessfulSearchDuration: Double?
    let activePersonClusterId: String; let activePersonSourceId: String
    let activeSourceContentId: String; let selectedPersonClusterId: String
    let selectedExportResults: [String: SearchResult]; let exportStatus: String
}
struct SearchMetadataFilters: Decodable {
    let mediaType: String?; let previewWindowMs: Int?; let pathPrefix: String?
    let sourceMtimeMin: Int?; let sourceMtimeMax: Int?
    let hasOcr: Bool?; let hasPerson: Bool?
}
struct SearchHistoryItem: Decodable, Identifiable {
    let queryId: String; let queryText: String; let filters: SearchMetadataFilters
    let resultCount: Int; let elapsedSeconds: Double; let createdAt: Double
    var id: String { queryId }
}
struct SavedSearchItem: Decodable, Identifiable {
    let savedSearchId: String; let displayName: String; let queryText: String
    let filters: SearchMetadataFilters; let updatedAt: Double
    var id: String { savedSearchId }
}
struct SearchMetadataResponse: Decodable {
    let status: String; let taskId: String
    let history: [SearchHistoryItem]; let savedSearches: [SavedSearchItem]
}
struct ActionResponse: Decodable {
    let status: String; let message: String?; let path: String?
    let identifier: String?; let error: String?
}
enum SnapshotReadDisposition: Equatable {
    case stale, cancelled, incomplete, failed, decoded
    static func resolve(status:String?, requested:Int, current:Int) -> SnapshotReadDisposition {
        guard requested == current else { return .stale }
        switch status {
        case "CANCELLED": return .cancelled
        case "INCOMPLETE": return .incomplete
        case "FAIL": return .failed
        default: return .decoded
        }
    }
}

struct ErrorResponse: Decodable {
    let status: String; let error: String?; let errorName: String?; let errorReason: String?
    let logPath: String?; let resultPath: String?; let diagnostic: String?; let exitCode: Int?
    var displayMessage: String {
        [errorName ?? error, errorReason].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: "：")
    }
    var diagnosticText: String {
        [
            logPath.map { "日志路径：\($0)" },
            resultPath.map { "结果文件路径：\($0)" },
            exitCode.map { "退出码：\($0)" },
            diagnostic,
        ].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: "\n")
    }
}

enum ArchivePage: String, CaseIterable, Identifiable {
    case newTask = "新建任务", running = "运行状态", history = "任务历史"
    case search = "搜索素材", favorites = "我的收藏", editorial = "文稿选片"
    case reference = "剪辑参考", groups = "素材分组", settings = "设置"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .newTask: return "plus.circle"
        case .running: return "play.circle"
        case .history: return "clock"
        case .search: return "magnifyingglass"
        case .favorites: return "heart.fill"
        case .editorial: return "list.bullet.rectangle"
        case .reference: return "book.closed"
        case .groups: return "square.stack.3d.up"
        case .settings: return "gearshape"
        }
    }
}

private func build151LifecycleEvent(_ name:String){
    let path=URL(fileURLWithPath:phaseCStateRoot).appendingPathComponent("logs/native-lifecycle.log")
    try? FileManager.default.createDirectory(at:path.deletingLastPathComponent(),withIntermediateDirectories:true)
    let line="\(Date().timeIntervalSince1970) pid=\(ProcessInfo.processInfo.processIdentifier) mono=\(Double(DispatchTime.now().uptimeNanoseconds)/1_000_000_000) \(name)\n"
    if !FileManager.default.fileExists(atPath:path.path){FileManager.default.createFile(atPath:path.path,contents:nil)}
    if let h=try? FileHandle(forWritingTo:path){h.seekToEndOfFile();h.write(Data(line.utf8));try? h.close()}
}
private enum Build151ChildOwner {
    static let lock=NSLock()
    static var closing=false
    static var children:[Int32:Process]=[:]
    static func register(_ p:Process){if p.arguments?.last=="shutdown" {return};lock.lock();children[p.processIdentifier]=p;let stop=closing;lock.unlock();if stop && p.isRunning {p.terminate()}}
    static func finish(_ p:Process){lock.lock();children.removeValue(forKey:p.processIdentifier);lock.unlock()}
    static func begin(){lock.lock();closing=true;let active=Array(children.values);lock.unlock();for p in active where p.isRunning {p.terminate()}}
}

final class ArchiveModel: ObservableObject {
    @Published var editorialSession: EditorialSession?
    @Published var editorialScript = ""
    @Published var editorialProjectPath = ""
    @Published var editorialBusy = false
    @Published var editorialStatus = ""
    @Published var page: ArchivePage = .newTask {didSet {if oldValue != page {searchPageChanged()}}}
    @Published var snapshot: Snapshot?
    @Published var loadError = ""
    @Published var snapshotNotice = ""
    private var lastTrustedSnapshotAt: Date?
    @Published var libraryCatalog:[ExistingLibrary]=[]
    @Published var catalogError=""
    func loadLibraryCatalog(){guard !terminating else{return};runHelper(["library-catalog"]){data,error in
        struct Catalog:Decodable {let status:String;let libraries:[ExistingLibrary]}
        if let data,let r=try? self.decoder().decode(Catalog.self,from:data),r.status=="PASS" {self.libraryCatalog=r.libraries;self.catalogError=""} else {self.catalogError=error ?? "Registry entry invalid"}
    }}
    @Published var query = ""
    @Published var mediaType = "全部"
    @Published var searchIntent = "visual" {didSet {if oldValue != searchIntent && page == .search {searchPageChanged()}}}
    @Published var previewWindow = "10 秒"
    @Published var searchPathPrefix = ""
    @Published var searchDateFrom = ""
    @Published var searchDateTo = ""
    @Published var searchRequireOCR = false
    @Published var searchRequirePerson = false
    @Published var selectedSearchLibraries: Set<String> = [] {didSet {if oldValue != selectedSearchLibraries {searchScopeChanged()}}}
    @Published var searching = false
    @Published var searchStatus = ""
    @Published var searchDiagnostic = ""
    @Published var searchLibraryNotices: [String] = []
    @Published var searchResults: [SearchResult] = []
    @Published var searchCoverage: SearchCoverage?
    @Published var searchTotalCount = 0
    @Published var nextSearchOffset: Int?
    @Published var lastSearchSignature = ""
    @Published var searchProgress: SearchProgressEvent?
    @Published var searchElapsedSeconds = 0.0
    @Published var searchCancelling = false
    @Published var searchPrewarmStatus = "搜索模型将在后台预热"
    @Published var searchPrewarmReady = false
    @Published var searchHistory: [SearchHistoryItem] = []
    @Published var savedSearches: [SavedSearchItem] = []
    @Published var savedSearchName = ""
    @Published var searchMetadataStatus = ""
    @Published var favoriteResults: [SearchResult] = []
    @Published var favoriteLoading = false
    @Published var favoriteStatus = ""
    @Published var selectedExportResults: [String: SearchResult] = [:]
    @Published var exportStatus = ""
    @Published var activePersonClusterId = ""
    @Published var activePersonSourceId = ""
    @Published var activeSourceContentId = ""
    @Published var searchNavigationDepth = 0
    @Published var searchNavigationTitle = ""
    @Published var selectedPersonClusterId = ""
    @Published var personClusterCatalog: [PersonClusterSummary] = []
    @Published var personClusterLoading = false
    @Published var personCapabilityNote = ""
    @Published var personDisplayName = ""
    @Published var personTags = ""
    @Published var personMergeTargetId = ""
    @Published var personEditStatus = ""
    @Published var showOriginalContent = false
    @Published var spokenCatalogue: SpokenCatalogue?
    @Published var spokenLoading = false
    @Published var spokenError = ""
    @Published var spokenKind = "all"
    @Published var spokenPersonOnly = false
    @Published var spokenClusterId = ""
    private var spokenGeneration = 0
    private var spokenLibraryGeneration = -1
    func loadSpokenCatalogue(kind: String = "all", personOnly: Bool = false, offset: Int = 0) {
        guard !personOnly || !selectedPersonClusterId.isEmpty else { return }
        spokenGeneration += 1
        let generation = spokenGeneration; let library = libraryGeneration
        spokenKind = kind; spokenPersonOnly = personOnly; spokenClusterId = personOnly ? selectedPersonClusterId : ""; spokenLoading = true; spokenError = ""
        spokenCatalogue = nil; spokenLibraryGeneration = library
        var arguments = ["spoken-content", "--content-kind", kind, "--result-offset", String(offset)]
        if personOnly { arguments += ["--cluster-id", selectedPersonClusterId] }
        runHelper(arguments) { data, error in
            guard generation == self.spokenGeneration && library == self.libraryGeneration else { return }
            self.spokenLoading = false
            guard let data else { self.spokenError = error ?? "转写目录读取失败"; return }
            do {
                let report = try self.decoder().decode(SpokenCatalogue.self, from: data)
                guard report.contract == "media_archive_spoken_catalog_v1", report.status == "PASS" else {
                    self.spokenError = report.message; return
                }
                self.spokenCatalogue = report
            } catch { self.spokenError = "转写目录读取失败：" + error.localizedDescription }
        }
    }
    var spokenScopeIsCurrent: Bool { spokenLibraryGeneration == libraryGeneration && (!spokenPersonOnly || spokenClusterId == selectedPersonClusterId) }
    var currentSpokenCatalogue: SpokenCatalogue? { spokenScopeIsCurrent ? spokenCatalogue : nil }
    @Published var videoContentTimeline: VideoContentTimeline?
    @Published var videoContentLoading = false
    @Published var videoContentError = ""
    @Published var videoContentSheetPresented = false
    @Published var videoContentSourceId = ""
    var videoContentExpectedRevision: String?
    private var videoContentGeneration = 0
    @Published var sourceFolder = ""
    @Published var libraryFolder = ""
    @Published var taskName: String = {
        let formatter = DateFormatter(); formatter.dateFormat = "yyyyMMdd"
        return "素材整理_" + formatter.string(from: Date())
    }()
    @Published var taskMode = "第一次完整整理"
    @Published var selectedExistingTaskPath = ""
    @Published var schedulerMode = "自动选择（推荐）"
    @Published var modelWorkers = 1
    @Published var frameWorkers = 1
    @Published var frameInterval = "3 秒"
    @Published var highValueMode = "目标 15%"
    @Published var imageScope = "按当前规则筛选图片"
    @Published var yoloeACoreText = ""
    @Published var yoloeBExtendedText = ""
    @Published var yoloeEnableBExtended = false
    @Published var yoloeDefaultACoreText = ""
    @Published var yoloeDefaultBExtendedText = ""
    @Published var yoloeDefaultEnableBExtended = false
    @Published var modelRoot = ""
    @Published var actionMessage = ""
    @Published var actionFailed = false
    @Published var actionInProgress = false {didSet {if actionInProgress {stopSearchPreparation()}}}
    @Published var historyDetail: TaskDetailResponse?
    @Published var historyLoading = false
    @Published var runtimeSourceDetail: RuntimeSourceDetail?
    @Published var runtimeSourceError = ""
    @Published var historyError = ""
    @Published var storageAudit: StorageAuditResponse?
    @Published var storageAuditError = ""
    @Published var storageCleanupPlan: StorageCleanupPlan?
    @Published var storageCleanupConfirmation = ""
    @Published var storageCleanupResult = ""
    @Published var comparisonLeftTaskPath = ""
    @Published var comparisonRightTaskPath = ""
    @Published var taskComparison: TaskComparisonResponse?
    private var videoPreviewControllers: [NSWindowController] = []
    private var activeSearchProcess: Process?
    private var searchRequestGeneration = 0
    private var searchElapsedTimer: Timer?
    private var searchMeasurementID = ""
    private var searchFirstResultRecorded = false
    private var searchFirstVisibleRecorded = false
    private var searchStartedAt: TimeInterval?
    private var bufferedSearchResults: [SearchResult] = []
    private var serverNextSearchOffset: Int?
    private var lastSuccessfulSearchDuration: Double?
    private var lastSearchMediaSummary = ""
    private var searchNavigationStack: [SearchNavigationSnapshot] = []
    private var searchPrewarmAttempted = false

    let taskModes = ["第一次完整整理", "增量整理", "修复缺失内容", "重建搜索入口", "补充音频搜索"]

    var taskActionTitle: String {
        switch taskMode {
        case "增量整理（尚未开放）": return "增量整理尚未开放"
        case "增量整理": return "开始增量整理"
        case "修复缺失内容": return "开始修复缺失内容"
        case "重建搜索入口": return "开始重建搜索入口"
        case "补充音频搜索": return "开始补充音频搜索"
        default: return "开始第一次完整整理"
        }
    }

    var taskModeExplanation: String {
        switch taskMode {
        case "增量整理（尚未开放）": return "计划用于扫描新增或变化素材并复用已有结果；当前版本尚未开放，避免误以为已可安全使用。"
        case "增量整理": return "在原素材库中重新对账，只处理新增、变更或缺少结果的素材；已完成且有效的结果不会重跑。"
        case "修复缺失内容": return "逐阶段核对数据库与正式产物；只补缺失或无效结果，已有成功记录不重跑。"
        case "重建搜索入口": return "只复用现有描述、OCR、标签和向量，重建数据库搜索入口；不读取原始素材，也不运行识别模型。"
        case "补充音频搜索": return "只读取所选索引中的视频，提取人声、转写文字并建立音频文本向量；不会重跑前19阶段。临时音频在逐视频写库后立即删除，只保留文本、时间点和向量。"
        default: return "从素材扫描开始建立一个新的完整素材库。"
        }
    }

    private var helperURL: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/素材大整理Python") }
    private var configURL: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/app_config.json") }
    @Published var terminating = false
    private var activeHelperCount = 0
    private var refreshTimer: Timer?
    private var lastSnapshotRequestAt = Date.distantPast
    private var snapshotGeneration = 0
    private var libraryGeneration = 0
    private var snapshotRequestInFlight = false
    private var integrityAuditInFlight = false
    private let snapshotSession=Build151SearchSessionTransport(helperScript:"build151_snapshot_session.py")
    private var activeStatusSnapshotProcess:Process?=nil
    func cancelRuntimeSnapshotReads(){
        guard page == .running else{return}
        snapshotGeneration += 1;snapshotRequestInFlight=false
        activeStatusSnapshotProcess?.terminate();activeStatusSnapshotProcess=nil
    }
    init() {
        loadSnapshot()
        loadLibraryCatalog()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            guard let self, !self.terminating, self.page == .running || self.page == .search || !(self.snapshot?.activeRuns.isEmpty ?? true) else { return }
            let minimumInterval = NSApp.isActive ? 10.0 : 15.0
            guard Date().timeIntervalSince(self.lastSnapshotRequestAt) >= minimumInterval else { return }
            self.lastSnapshotRequestAt = Date()
            self.loadSnapshot()
        }
    }
    deinit {
        refreshTimer?.invalidate()
        searchElapsedTimer?.invalidate()
    }

    private func decoder() -> JSONDecoder {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase; return decoder
    }

    private func runHelper(_ arguments: [String], completion: @escaping (Data?, String?) -> Void) {
        let unavailable: Set<String> = ["search-metadata","save-search","annotate-source","favorites","person-track-suggestions","person-name","person-merge","person-detach","person-create","person-add-visual","person-remove-visual","storage-cleanup-apply","storage-cleanup-plan","retry-one-failed-qwen","switch-scheduler"]
        if let command = arguments.first, unavailable.contains(command) {
            completion(nil, "暂不可用：当前稳定产品边界尚未提供；未调用旧实现")
            return
        }

        if arguments == ["snapshot"] {
            activeHelperCount += 1
            let contents=Bundle.main.bundleURL.appendingPathComponent("Contents");let config=configURL
            DispatchQueue.global(qos:.utility).async {
                do {
                    let data=try self.snapshotSession.query(contents:contents,config:config,environment:phaseCEnvironment(),arguments:arguments,queryId:UUID().uuidString,started:{ p in
                        DispatchQueue.main.async {self.activeStatusSnapshotProcess=p;if self.page == .running {self.cancelRuntimeSnapshotReads()}}
                    },progress:{ _ in })
                    DispatchQueue.main.async {self.activeHelperCount -= 1;completion(data,nil)}
                } catch {
                    DispatchQueue.main.async {self.activeHelperCount -= 1;completion(nil,error.localizedDescription)}
                }
            }
            return
        }
        activeHelperCount += 1
        let config = configURL
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process(); let output = Pipe(); let errors = Pipe()
            process.environment = phaseCEnvironment()
            let statusRead=arguments.first=="snapshot"
            let contents=Bundle.main.bundleURL.appendingPathComponent("Contents")
            let lifecycle=["library-catalog","shutdown"].contains(arguments.first ?? "")
            let script=contents.appendingPathComponent(statusRead ? "Resources/build151_snapshot_lifecycle.py":lifecycle ? "Resources/build151_lifecycle_cli.py":"Resources/python_bridge.py")
            process.executableURL=contents.appendingPathComponent("Helpers/PipelinePython/system")
            process.arguments=[contents.appendingPathComponent("Resources/build151_owned_helper.py").path,script.path,"--config",config.path]+arguments
            process.standardOutput = output; process.standardError = errors
            do {
                try process.run(); Build151ChildOwner.register(process)
                if statusRead {DispatchQueue.main.async {self.activeStatusSnapshotProcess=process;if self.page == .running {self.cancelRuntimeSnapshotReads()}}}
                let data = output.fileHandleForReading.readDataToEndOfFile()
                let errorData = errors.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit(); Build151ChildOwner.finish(process)
                let decodedError = try? self.decoder().decode(ErrorResponse.self, from: data)
                let message = process.terminationStatus == 0 ? nil : (
                    decodedError?.displayMessage ?? String(data: errorData, encoding: .utf8) ?? "辅助程序执行失败"
                )
                DispatchQueue.main.async { self.activeHelperCount -= 1; completion(data, message) }
            } catch { DispatchQueue.main.async { self.activeHelperCount -= 1; completion(nil, error.localizedDescription) } }
        }
    }

    private func runPhaseCRead(_ arguments:[String], completion:@escaping(Data?,String?)->Void) {
        let contents=Bundle.main.bundleURL.appendingPathComponent("Contents")
        DispatchQueue.global(qos:.utility).async {
            let p=Process();let out=Pipe();let err=Pipe()
            p.executableURL=contents.appendingPathComponent("Helpers/PipelinePython/system")
            p.arguments=[contents.appendingPathComponent("Resources/phasec_view_api.py").path]+arguments
            p.environment=phaseCEnvironment();p.standardOutput=out;p.standardError=err
            do {try p.run(); Build151ChildOwner.register(p);let data=out.fileHandleForReading.readDataToEndOfFile();let errors=err.fileHandleForReading.readDataToEndOfFile();p.waitUntilExit(); Build151ChildOwner.finish(p)
                DispatchQueue.main.async {completion(data,p.terminationStatus==0 ? nil : String(data:errors,encoding:.utf8) ?? "读取失败")}
            } catch {DispatchQueue.main.async {completion(nil,error.localizedDescription)}}
        }
    }
    func loadRuntimeSource(_ sourceId: String) {
        runtimeSourceDetail = nil; runtimeSourceError = ""
        runPhaseCRead(["source-detail", "--source-id", sourceId]) { data, error in
            if let error = error { self.runtimeSourceError = error; return }
            guard let data = data else { self.runtimeSourceError = "未收到来源详情"; return }
            do { self.runtimeSourceDetail = try self.decoder().decode(RuntimeSourceDetail.self, from: data) }
            catch { self.runtimeSourceError = "来源详情读取失败：\(error)" }
        }
    }
    private let querySession=Build151SearchSessionTransport()
    @Published var semanticPreparation = ""
    @Published var libraryPreparation:[String:String]=[:]
    private var preparationGeneration=0
    private var preparationBusy=false
    private var preparationTimer:Timer?
    private var preparationProcess:Process?
    private var semanticPrepared=false
    func stopSearchPreparation() {
        searchRequestGeneration += 1
        activeSearchProcess?.terminate();activeSearchProcess=nil
        if searching {searching=false;searchCancelling=false;stopSearchTimer();searchProgress=nil;searchStatus="本次搜索已取消；已有结果未受影响"}
        preparationGeneration += 1;preparationTimer?.invalidate();preparationTimer=nil
        preparationProcess?.terminate();preparationProcess=nil
        preparationBusy=false;semanticPrepared=false;semanticPreparation=""
    }
    func searchPageChanged() {
        build151LifecycleEvent("search_page_changed page=\(page.rawValue)")
        stopSearchPreparation()
        guard page == .search, !terminating, !actionInProgress else{return}
        requestSearchPreparation()
        preparationTimer=Timer.scheduledTimer(withTimeInterval:15,repeats:true){[weak self] _ in self?.requestSearchPreparation(lease:true)}
    }
    func searchScopeChanged() {
        searchRequestGeneration += 1;preparationGeneration += 1
        if searching || preparationBusy {activeSearchProcess?.terminate();preparationProcess?.terminate()}
        activeSearchProcess=nil;searching=false;searchCancelling=false;stopSearchTimer();searchProgress=nil
        preparationBusy=false;semanticPrepared=false;semanticPreparation="";libraryPreparation=[:]
        searchResults=[];bufferedSearchResults=[];searchTotalCount=0;nextSearchOffset=nil;serverNextSearchOffset=nil;lastSearchSignature=""
        searchStatus="搜索范围已切换，请点击搜索获取此范围的结果";searchDiagnostic="";searchElapsedSeconds=0;lastSearchMediaSummary=""
        if page == .search && !terminating {requestSearchPreparation()}
    }
    private func requestSearchPreparation(lease:Bool=false) {
        guard page == .search, !terminating, !actionInProgress, !preparationBusy, !searching else{return}
        preparationBusy=true
        let generation=preparationGeneration;let library=libraryGeneration
        let contents=Bundle.main.bundleURL.appendingPathComponent("Contents");let config=configURL
        let operation=lease ? "lease":"prepare";let intent=searchIntent
        let targets=selectedSearchLibraries.isEmpty ? Set(snapshot?.existingLibraries.filter{$0.isActive}.map{$0.taskPath} ?? []) : selectedSearchLibraries
        let paths=lease ? [""] : (selectedSearchLibraries.isEmpty ? [""] : selectedSearchLibraries.sorted())
        if !lease {semanticPreparation="正在异步准备语义搜索…";for path in paths {libraryPreparation[path]="准备中"}}
        DispatchQueue.global(qos:.utility).async {
            var refreshChanged=false
            for path in paths {
                guard DispatchQueue.main.sync(execute:{generation==self.preparationGeneration && self.page == .search && !self.terminating && !self.searching}) else{break}
                let qid=UUID().uuidString
                var response:[String:Any]?;var failure:String?
                do {
                    let arguments=["search","--query","","--search-intent",intent]+(path.isEmpty ? []:["--library-task-path",path])
                    let data=try self.querySession.query(contents:contents,config:config,environment:phaseCEnvironment(),arguments:arguments,queryId:qid,operation:operation,isAllowed:{DispatchQueue.main.sync {generation==self.preparationGeneration && self.page == .search && !self.terminating}},started:{p in
                        DispatchQueue.main.async {if generation==self.preparationGeneration {self.preparationProcess=p} else {p.terminate()}}
                    },progress:{_ in})
                    response=(try JSONSerialization.jsonObject(with:data)) as? [String:Any]
                } catch {failure=error.localizedDescription}
                let result=response;let message=failure
                if lease,let states=result?["library_states"] as? [[String:Any]] {
                    refreshChanged=states.contains{targets.contains($0["task_path"] as? String ?? "") && $0["reason"] as? String == "数据库或配置已变化，需要重新准备"}
                }
                DispatchQueue.main.async {
                    guard generation==self.preparationGeneration,library==self.libraryGeneration,self.page == .search else{return}
                    build151LifecycleEvent("search_preparation_result operation=\(operation) id=\(qid)")
                    if let states=result?["library_states"] as? [[String:Any]] {
                        for state in states {
                            let key=state["task_path"] as? String ?? "";let status=state["state"] as? String ?? "unavailable"
                            self.libraryPreparation[key]=status=="ready" ? "索引已就绪" : ((status=="pending" ? "待准备":"不可用")+"："+(state["reason"] as? String ?? ""))
                        }
                    }
                    if result?["status"] as? String != "PASS" {self.libraryPreparation[path]="不可用："+(message ?? result?["error_reason"] as? String ?? "准备未完成")}
                    self.semanticPrepared=result?["status"] as? String == "PASS" && result?["semantic_ready"] as? Bool == true
                    self.semanticPreparation=self.semanticPrepared ? (self.selectedSearchLibraries.count > 1 ? "查询模型已就绪；不同兼容组按需排队" : "语义搜索已就绪") : (message ?? result?["error_reason"] as? String ?? "语义搜索尚未就绪，搜索时按需准备")
                }
            }
            let refresh=refreshChanged
            DispatchQueue.main.async {if generation==self.preparationGeneration {self.preparationBusy=false;if refresh {self.requestSearchPreparation()}}}
        }
    }


    private func runSearchHelper(
        _ arguments: [String],
        progress: @escaping (SearchProgressEvent) -> Void,
        completion: @escaping (Data?, String?) -> Void
    ) {
        if arguments.first == "search" {
            let config=configURL;let generation=libraryGeneration;let prepGeneration=preparationGeneration;let queryId=searchMeasurementID;let queryDeadline=ProcessInfo.processInfo.systemUptime+45
            searchRequestGeneration += 1;let requestGeneration=searchRequestGeneration
            let contents=Bundle.main.bundleURL.appendingPathComponent("Contents")
            DispatchQueue.global(qos:.userInitiated).async {
                do {
                    let data=try self.querySession.query(contents:contents,config:config,environment:phaseCEnvironment(),arguments:arguments,queryId:queryId,deadlineMono:queryDeadline,isAllowed:{DispatchQueue.main.sync {generation==self.libraryGeneration && prepGeneration==self.preparationGeneration && requestGeneration==self.searchRequestGeneration && self.page == .search && !self.terminating}},started:{ process in
                        DispatchQueue.main.async {if generation==self.libraryGeneration && requestGeneration==self.searchRequestGeneration {self.activeSearchProcess=process} else {process.terminate()}}
                    },progress:{ data in
                        guard let event=try? self.decoder().decode(SearchProgressEvent.self,from:data) else {return}
                        DispatchQueue.main.async {if generation==self.libraryGeneration && requestGeneration==self.searchRequestGeneration && self.searching && !self.searchCancelling {progress(event)}}
                    })
                    DispatchQueue.main.async {
                        guard generation==self.libraryGeneration && requestGeneration==self.searchRequestGeneration else {return}
                        self.activeSearchProcess=nil;completion(data,nil)
                    }
                } catch {
                    DispatchQueue.main.async {
                        guard generation==self.libraryGeneration && requestGeneration==self.searchRequestGeneration else {return}
                        self.activeSearchProcess=nil;completion(nil,error.localizedDescription)
                    }
                }
            }
            return
        }
        let config = configURL
        let requestedLibrary = libraryGeneration
        searchRequestGeneration += 1
        let requestedSearch = searchRequestGeneration
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process(); let output = Pipe(); let errors = Pipe()
            process.environment = phaseCEnvironment()
            let contents=Bundle.main.bundleURL.appendingPathComponent("Contents")
            process.executableURL=contents.appendingPathComponent("Helpers/PipelinePython/system")
            process.arguments=[contents.appendingPathComponent("Resources/build151_owned_helper.py").path,contents.appendingPathComponent("Resources/python_bridge.py").path,"--config",config.path]+arguments
            process.standardOutput = output; process.standardError = errors
            do {
                try process.run(); Build151ChildOwner.register(process)
                DispatchQueue.main.async {
                    if requestedLibrary == self.libraryGeneration && requestedSearch == self.searchRequestGeneration && self.searching { self.activeSearchProcess = process }
                }
                let readGroup = DispatchGroup()
                let lock = NSLock()
                var outputData = Data()
                var errorData = Data()
                readGroup.enter()
                DispatchQueue.global(qos: .userInitiated).async {
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    lock.lock(); outputData = data; lock.unlock()
                    readGroup.leave()
                }
                readGroup.enter()
                DispatchQueue.global(qos: .userInitiated).async {
                    var pending = Data()
                    while true {
                        let chunk = errors.fileHandleForReading.availableData
                        if chunk.isEmpty { break }
                        pending.append(chunk)
                        lock.lock()
                        errorData.append(chunk)
                        if errorData.count > 32 * 1024 {
                            errorData = errorData.suffix(32 * 1024)
                        }
                        lock.unlock()
                        while let newline = pending.firstIndex(of: 10) {
                            let lineData = pending.prefix(upTo: newline)
                            pending.removeSubrange(...newline)
                            guard let line = String(data: lineData, encoding: .utf8),
                                  line.hasPrefix("SEARCH_PROGRESS_JSON="),
                                  let jsonData = String(
                                    line.dropFirst("SEARCH_PROGRESS_JSON=".count)
                                  ).data(using: .utf8),
                                  let event = try? self.decoder().decode(
                                    SearchProgressEvent.self, from: jsonData
                                  )
                            else { continue }
                            DispatchQueue.main.async {
                                if requestedLibrary == self.libraryGeneration && requestedSearch == self.searchRequestGeneration && self.searching && !self.searchCancelling { progress(event) }
                            }
                        }
                    }
                    readGroup.leave()
                }
                process.waitUntilExit(); Build151ChildOwner.finish(process)
                readGroup.wait()
                lock.lock()
                let finalOutput = outputData
                lock.unlock()
                let decodedError = try? self.decoder().decode(
                    ErrorResponse.self, from: finalOutput
                )
                let message = process.terminationStatus == 0 ? nil : (
                    decodedError?.displayMessage
                    ?? "搜索未完成（退出码 \(process.terminationStatus)）；已有结果保留，请查看诊断日志"
                )
                DispatchQueue.main.async {
                    guard requestedLibrary == self.libraryGeneration && requestedSearch == self.searchRequestGeneration else { return }
                    if self.activeSearchProcess === process {
                        self.activeSearchProcess = nil
                    }
                    completion(finalOutput, message)
                }
            } catch {
                DispatchQueue.main.async {
                    guard requestedLibrary == self.libraryGeneration && requestedSearch == self.searchRequestGeneration else { return }
                    if self.activeSearchProcess === process {
                        self.activeSearchProcess = nil
                    }
                    completion(nil, error.localizedDescription)
                }
            }
        }
    }

    private func startSearchTimer() {
        searchElapsedTimer?.invalidate()
        searchStartedAt = Double(DispatchTime.now().uptimeNanoseconds)/1_000_000_000
        searchMeasurementID = UUID().uuidString
        searchFirstResultRecorded = false
        searchFirstVisibleRecorded = false
        build151LifecycleEvent("search_begin id=\(searchMeasurementID) mode=\(searchIntent)")
        searchElapsedSeconds = 0
        searchElapsedTimer = Timer.scheduledTimer(
            withTimeInterval: 0.2, repeats: true
        ) { [weak self] _ in
            guard let self, let started = self.searchStartedAt else { return }
            self.searchElapsedSeconds = (Double(DispatchTime.now().uptimeNanoseconds)/1_000_000_000 - started)
        }
    }

    private func stopSearchTimer() {
        searchElapsedTimer?.invalidate()
        searchElapsedTimer = nil
        if let started = searchStartedAt {
            searchElapsedSeconds = (Double(DispatchTime.now().uptimeNanoseconds)/1_000_000_000 - started)
        }
        searchStartedAt = nil
    }

    func cancelSearch() {
        guard searching else {return}
        searchRequestGeneration += 1;preparationGeneration += 1
        if let process=activeSearchProcess,process.isRunning {process.terminate()}
        if let process=preparationProcess,process !== activeSearchProcess,process.isRunning {process.terminate()}
        activeSearchProcess=nil;preparationProcess=nil;preparationBusy=false
        semanticPrepared=false;semanticPreparation="语义搜索已释放，下次搜索时恢复"
        searching=false;searchCancelling=false;stopSearchTimer();searchProgress=nil
        lastSearchSignature="";serverNextSearchOffset=nil;nextSearchOffset=nil
        searchStatus="已取消本次搜索；已有结果未受影响"
    }

    func prewarmSearch() {
        if snapshot?.searchRuntime.searchMode == "V2_CURRENT_TEXT" || snapshot?.searchRuntime.searchMode == "V2_CURRENT_HYBRID" {
            searchPrewarmReady = true
            searchPrewarmStatus = snapshot?.searchRuntime.searchMode == "V2_CURRENT_HYBRID" ? "按需加载；短时复用，闲置或内存压力时释放" : "当前结果文字搜索无需加载模型"
            return
        }
        guard !terminating, !searchPrewarmAttempted, snapshot?.searchRuntime.ready == true else { return }
        searchPrewarmAttempted = true
        searchPrewarmStatus = "正在后台准备本地搜索模型…"
        runHelper(["search-prewarm"]) { data, error in
            if let data,
               let response = try? self.decoder().decode(ActionResponse.self, from: data),
               response.status == "PASS" {
                self.searchPrewarmReady = true
                self.searchPrewarmStatus = response.message ?? "搜索模型已预热"
            } else {
                self.searchPrewarmReady = false
                self.searchPrewarmStatus = "预热未完成；搜索仍可自动加载模型"
                if let error, !error.isEmpty { self.searchDiagnostic = error }
            }
        }
    }

    private func startIntegrityAudit() {
        guard !integrityAuditInFlight, !terminating else {return}
        integrityAuditInFlight = true
        searchStatus = "正在后台完整校验素材库；处理任务可继续运行"
        let contents=Bundle.main.bundleURL.appendingPathComponent("Contents"); let config=configURL
        DispatchQueue.global(qos:.utility).async {
            let p=Process(); let out=Pipe(); let err=Pipe()
            p.executableURL=contents.appendingPathComponent("Helpers/PipelinePython/system")
            p.arguments=[contents.appendingPathComponent("Resources/build151_integrity_audit.py").path,"--config",config.path,"integrity-audit"]
            p.environment=phaseCEnvironment();p.standardOutput=out;p.standardError=err
            var failure:String?=nil
            do {
                try p.run();Build151ChildOwner.register(p)
                let data=out.fileHandleForReading.readDataToEndOfFile()
                let errors=err.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit();Build151ChildOwner.finish(p)
                if p.terminationStatus != 0 {failure=String(data:errors.isEmpty ? data:errors,encoding:.utf8) ?? "数据库校验失败"}
            } catch {failure=error.localizedDescription}
            let message=failure
            DispatchQueue.main.async {
                self.integrityAuditInFlight=false
                guard !self.terminating else {return}
                if let message {self.loadError=message}
                self.loadSnapshot()
            }
        }
    }

    func loadSnapshot() {
        guard page != .running else {return}
        guard !snapshotRequestInFlight, !terminating else { return }
        snapshotRequestInFlight = true
        snapshotGeneration += 1
        let requestedGeneration = snapshotGeneration
        runHelper(["snapshot"]) { data, error in
            guard SnapshotReadDisposition.resolve(status:nil,requested:requestedGeneration,current:self.snapshotGeneration) != .stale else { return }
            self.snapshotRequestInFlight = false
            guard let data else {
                self.loadError = error ?? "无法读取中心数据库状态"
                return
            }
            do {
                if let outcome = try? JSONSerialization.jsonObject(with: data) as? [String:Any],
                   let status = outcome["status"] as? String, [.cancelled, .incomplete].contains(SnapshotReadDisposition.resolve(status:status,requested:requestedGeneration,current:self.snapshotGeneration)) {
                    let updated = self.lastTrustedSnapshotAt.map { "；显示上次检查状态（\(DateFormatter.localizedString(from:$0,dateStyle:.none,timeStyle:.medium))）" } ?? "；尚未取得当前状态"
                    self.snapshotNotice = (status == "INCOMPLETE" ? "素材库检查暂未完成，可稍后重试" : "本次检查已取消") + updated
                    return
                }
                if let failure=try? self.decoder().decode(ErrorResponse.self,from:data),failure.status=="FAIL",(try? self.decoder().decode(Snapshot.self,from:data)) == nil {self.loadError=failure.displayMessage;return}
                let state = try self.decoder().decode(Snapshot.self, from: data)
                let firstLoad = self.snapshot == nil
                let wasSearchReady = self.snapshot?.pipeline.searchReady == true
                let wasPipelineComplete = self.snapshot?.pipeline.fullPipelineLauncherStatus == "SUCCESS"
                self.snapshot = state; self.loadError = ""; self.snapshotNotice = ""; self.lastTrustedSnapshotAt = Date()
                if state.database.integrityCheck == "pending" {self.startIntegrityAudit()}
                if self.searchStatus.hasPrefix("正在读取所选素材库") || self.searchStatus.hasPrefix("正在后台完整校验素材库") {self.searchStatus=state.searchRuntime.ready ? "素材库已就绪":state.database.integrityCheck == "pending" ? "正在后台完整校验素材库；处理任务可继续运行":"素材库尚不可搜索，请查看读取状态"}
                if firstLoad {
                    self.modelRoot = state.runtimeContract.modelRoot ?? ""
                    if let profile = state.savedProfile {
                        self.modelWorkers = profile.scheduler.modelWorkers
                        self.frameWorkers = profile.scheduler.frameExtractWorkers
                        self.schedulerMode = [
                            "auto": "自动选择（推荐）",
                            "pipeline_async": "自定义（高级）",
                            "stage_serial": "自定义（高级）",
                        ][profile.scheduler.mode] ?? "自动选择（推荐）"
                        self.frameInterval = String(
                            format: "%.0f 秒",
                            profile.videoSampling.frameIntervalSeconds
                        )
                        self.highValueMode = [
                            "frozen_v25_compatible": "兼容当前规则",
                            "target_15": "目标 15%",
                            "target_20": "目标 20%",
                            "target_30": "目标 30%",
                        ][profile.highValuePolicy.mode] ?? "兼容当前规则"
                        self.imageScope = profile.highValuePolicy.imageScope == "all_images"
                            ? "所有普通图片都进入画面描述"
                            : "按当前规则筛选图片"
                    } else {
                        self.modelWorkers = state.hardware.recommendation.modelWorkers
                        self.frameWorkers = state.hardware.recommendation.frameExtractWorkers
                    }
                    self.yoloeACoreText = state.yoloeKeywordProfile.aCore.map(\.editableLine).joined(separator: "\n")
                    self.yoloeBExtendedText = state.yoloeKeywordProfile.bExtended.map(\.editableLine).joined(separator: "\n")
                    self.yoloeEnableBExtended = state.yoloeKeywordProfile.enableBExtended
                    self.yoloeDefaultACoreText = state.yoloeDefaultKeywordProfile.aCore.map(\.editableLine).joined(separator: "\n")
                    self.yoloeDefaultBExtendedText = state.yoloeDefaultKeywordProfile.bExtended.map(\.editableLine).joined(separator: "\n")
                    self.yoloeDefaultEnableBExtended = state.yoloeDefaultKeywordProfile.enableBExtended
                    self.selectedExistingTaskPath = state.existingLibraries.first?.taskPath ?? ""
                    if state.searchRuntime.ready { self.loadSearchMetadata() }
                }
                let pipelineJustCompleted = (
                    !wasPipelineComplete
                    && state.pipeline.fullPipelineLauncherStatus == "SUCCESS"
                )
                if !firstLoad && self.page == .running && state.pipeline.searchReady
                    && (!wasSearchReady || pipelineJustCompleted) {
                    self.actionMessage = "整理已经完成，图片与视频搜索现已开放"
                    self.page = .search
                }
            } catch {
                self.loadError = "状态数据解析失败：\(error.localizedDescription)"
            }
        }
    }

    func loadHistoryDetail(_ library: ExistingLibrary) {
        historyLoading = true; historyError = ""; historyDetail = nil
        runHelper(["task-detail", "--task", library.taskPath]) { data, error in
            self.historyLoading = false
            guard let data else { self.historyError = error ?? "无法读取历史任务明细"; return }
            do {
                self.historyDetail = try self.decoder().decode(TaskDetailResponse.self, from: data)
            } catch {
                self.historyError = error.localizedDescription
            }
        }
    }

    func loadStorageAudit(_ library: ExistingLibrary) {
        storageAuditError = "正在只读统计任务目录…"; storageAudit = nil
        runHelper(["storage-audit", "--task", library.taskPath]) { data, error in
            guard let data else { self.storageAuditError = error ?? "无法读取存储审计"; return }
            do {
                self.storageAudit = try self.decoder().decode(StorageAuditResponse.self, from: data)
                self.storageAuditError = ""
            } catch { self.storageAuditError = error.localizedDescription }
        }
    }

    func loadStorageCleanupPlan(_ library: ExistingLibrary) {
        storageAuditError = "正在生成只读清理计划…"
        storageCleanupPlan = nil; storageCleanupConfirmation = ""; storageCleanupResult = ""
        runHelper(["storage-cleanup-plan", "--task", library.taskPath]) { data, error in
            guard let data else { self.storageAuditError = error ?? "无法生成清理计划"; return }
            do {
                self.storageCleanupPlan = try self.decoder().decode(StorageCleanupPlan.self, from: data)
                self.storageAuditError = ""
            } catch { self.storageAuditError = error.localizedDescription }
        }
    }

    func applyStorageCleanup(_ library: ExistingLibrary) {
        guard let plan = storageCleanupPlan,
              storageCleanupConfirmation == plan.confirmationPhrase else {
            storageAuditError = "确认短语不匹配；没有删除任何内容"; return
        }
        storageAuditError = "正在重新核对计划并清理明确候选…"
        runHelper([
            "storage-cleanup-apply", "--task", library.taskPath,
            "--plan-id", plan.planId,
            "--confirmation-phrase", storageCleanupConfirmation,
        ]) { data, error in
            guard let data else { self.storageAuditError = error ?? "清理未执行"; return }
            do {
                let result = try self.decoder().decode(StorageCleanupResult.self, from: data)
                self.storageCleanupResult = "已删除 \(result.removedCount) 项，释放 \(formatBytes(result.removedBytes))；原始素材和任务数据库未触碰。"
                self.storageCleanupPlan = nil; self.storageCleanupConfirmation = ""
                self.storageAuditError = ""
                self.loadStorageAudit(library)
            } catch { self.storageAuditError = error.localizedDescription }
        }
    }

    func compareSelectedTasks() {
        guard !comparisonLeftTaskPath.isEmpty, !comparisonRightTaskPath.isEmpty,
              comparisonLeftTaskPath != comparisonRightTaskPath else {
            storageAuditError = "请选择两个不同的历史任务"; return
        }
        storageAuditError = "正在只读比较两个任务…"; taskComparison = nil
        runHelper([
            "compare-tasks", "--left-task", comparisonLeftTaskPath,
            "--right-task", comparisonRightTaskPath,
        ]) { data, error in
            guard let data else { self.storageAuditError = error ?? "无法比较任务"; return }
            do {
                self.taskComparison = try self.decoder().decode(TaskComparisonResponse.self, from: data)
                self.storageAuditError = ""
            } catch { self.storageAuditError = error.localizedDescription }
        }
    }

    func activateLibrary(_ library: ExistingLibrary) {
        runAction(
            ["activate-library", "--task", library.taskPath],
            pendingMessage: "正在打开现有项目…",
            successPage: .running,
            clearSearchOnSuccess: true
        )
    }

    func addProjectFolder(_ projectId:String) {
        let panel=NSOpenPanel();panel.canChooseDirectories=true;panel.canChooseFiles=false;panel.allowsMultipleSelection=false;panel.prompt="追加素材文件夹"
        guard panel.runModal() == .OK,let url=panel.url else{return}
        runAction(["add-project-folder","--folder",url.path,"--project-id",projectId],pendingMessage:"正在核验并追加素材；保留已有结果…",successPage:.running)
    }

    func chooseSourceFolder() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.allowsMultipleSelection = false; panel.prompt = "选择素材文件夹"
        if panel.runModal() == .OK { sourceFolder = panel.url?.path ?? "" }
    }

    func chooseLibraryFolder() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.canCreateDirectories = true; panel.allowsMultipleSelection = false
        panel.prompt = "选择或新建索引保存位置"
        if panel.runModal() == .OK { libraryFolder = panel.url?.path ?? "" }
    }

    func chooseModelRoot() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.canCreateDirectories = true; panel.allowsMultipleSelection = false
        panel.prompt = "选择模型总目录"
        if panel.runModal() == .OK { modelRoot = panel.url?.path ?? "" }
    }

    func saveModelRoot() {
        guard !modelRoot.isEmpty else {
            actionFailed = true; actionMessage = "请先选择模型总目录"; return
        }
        runAction(
            ["save-model-root", "--path", modelRoot],
            pendingMessage: "正在检查并保存模型位置…"
        )
    }

    func startTask() {
        guard ["第一次完整整理","增量整理"].contains(taskMode) else { actionFailed=true; actionMessage="当前模式不可用"; return }
        if taskMode != "第一次完整整理" {
            guard !selectedExistingTaskPath.isEmpty else {
                actionFailed = true; actionMessage = "请先选择一个已有素材库"; return
            }
            let modeMap = ["增量整理":"incremental", "修复缺失内容":"repair", "重建搜索入口":"rebuild_search", "补充音频搜索":"audio_enrichment"]
            runAction(
                ["start-existing-task", "--task", selectedExistingTaskPath, "--task-mode", modeMap[taskMode] ?? "repair"],
                pendingMessage: "正在准备\(taskMode)…",
                successPage: .running
            )
            return
        }
        let cleanName = taskName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !sourceFolder.isEmpty, !libraryFolder.isEmpty, !cleanName.isEmpty else {
            actionFailed = true; actionMessage = "请选择素材文件夹、索引保存位置并填写任务名称"; return
        }
        let modeMap = ["第一次完整整理":"full"]
        runAction(
            ["start-task", "--source", sourceFolder, "--workspace-root", libraryFolder, "--name", cleanName, "--task-mode", modeMap[taskMode] ?? "full"],
            pendingMessage: "正在准备\(taskMode)…",
            successPage: .running
        )
    }

    func stopTask() {
        runAction(["stop-task"], pendingMessage: "正在停止当前任务…")
    }

    func resumeTask() {
        runAction(["resume-task"], pendingMessage: "正在从断点继续…", successPage: .running)
    }

    func retryOneFailedDescription() {
        runAction(["retry-one-failed-qwen"], pendingMessage: "正在定向重试一项失败画面描述；成功项不重做…", successPage: .running)
    }

    func saveProfile() {
        let scheduler = ["自动选择（推荐)":"auto", "自动选择（推荐）":"auto", "自定义（高级）":"stage_serial"][schedulerMode] ?? "auto"
        let highValue = [
            "兼容当前规则":"frozen_v25_compatible",
            "目标 15%":"target_15",
            "目标 20%":"target_20",
            "目标 30%":"target_30",
        ][highValueMode] ?? "frozen_v25_compatible"
        let scope = (
            imageScope == "所有图片进入高价值分析"
            || imageScope == "所有普通图片都进入画面描述"
        ) ? "all_images" : "frozen_current_policy"
        let interval = frameInterval.split(separator: " ").first.map(String.init) ?? "3"
        runAction(["save-profile", "--scheduler-mode", scheduler, "--model-workers", String(modelWorkers), "--frame-extract-workers", String(frameWorkers), "--frame-interval-seconds", interval, "--high-value-mode", highValue, "--image-scope", scope, "--yoloe-a-keywords", yoloeACoreText, "--yoloe-b-keywords", yoloeBExtendedText, "--yoloe-enable-b-extended", yoloeEnableBExtended ? "true" : "false"], pendingMessage: "正在保存设置…")
    }

    func restoreDefaultYoloeKeywords() {
        yoloeACoreText = yoloeDefaultACoreText
        yoloeBExtendedText = yoloeDefaultBExtendedText
        yoloeEnableBExtended = yoloeDefaultEnableBExtended
        actionFailed = false
        actionMessage = "已恢复内置词表；点击“保存为今后任务的默认方案”后生效"
    }

    private func clearSearchForLibraryChange() {
        stopSearchPreparation()
        libraryGeneration += 1
        snapshotGeneration += 1
        snapshotRequestInFlight = false
        snapshot = nil; snapshotNotice = ""; loadError = ""; lastTrustedSnapshotAt = nil
        selectedSearchLibraries = []
        personClusterCatalog = []
        personClusterLoading = false
        personCapabilityNote = "正在读取当前素材库的人物分组…"
        activeSearchProcess?.terminate()
        activeSearchProcess = nil
        searching = false; searchCancelling = false; stopSearchTimer()
        searchResults = []; bufferedSearchResults = []
        searchCoverage = nil; searchTotalCount = 0; nextSearchOffset = nil
        serverNextSearchOffset = nil; lastSearchSignature = ""
        activePersonClusterId = ""; activePersonSourceId = ""
        activeSourceContentId = ""
        clearSearchNavigation()
        selectedPersonClusterId = ""; searchDiagnostic = ""
        searchHistory = []; savedSearches = []; savedSearchName = ""
        searchStatus = "正在读取所选素材库，请稍候…"
    }

    private func pushSearchNavigation(title: String) {
        guard !searchResults.isEmpty else { return }
        searchNavigationStack.append(SearchNavigationSnapshot(
            returnTitle: title,
            query: query, mediaType: mediaType, previewWindow: previewWindow,
            searchPathPrefix: searchPathPrefix, searchDateFrom: searchDateFrom,
            searchDateTo: searchDateTo, searchRequireOCR: searchRequireOCR,
            searchRequirePerson: searchRequirePerson,
            searchStatus: searchStatus, searchDiagnostic: searchDiagnostic,
            searchResults: searchResults, bufferedSearchResults: bufferedSearchResults,
            searchCoverage: searchCoverage, searchTotalCount: searchTotalCount,
            nextSearchOffset: nextSearchOffset, serverNextSearchOffset: serverNextSearchOffset,
            lastSearchSignature: lastSearchSignature,
            lastSearchMediaSummary: lastSearchMediaSummary,
            lastSuccessfulSearchDuration: lastSuccessfulSearchDuration,
            activePersonClusterId: activePersonClusterId,
            activePersonSourceId: activePersonSourceId,
            activeSourceContentId: activeSourceContentId,
            selectedPersonClusterId: selectedPersonClusterId,
            selectedExportResults: selectedExportResults, exportStatus: exportStatus
        ))
        searchNavigationDepth = searchNavigationStack.count
        searchNavigationTitle = title
    }

    private func clearSearchNavigation() {
        searchNavigationStack.removeAll()
        searchNavigationDepth = 0
        searchNavigationTitle = ""
    }

    func navigateBackInSearch() {
        guard !searching, let snapshot = searchNavigationStack.popLast() else { return }
        query = snapshot.query; mediaType = snapshot.mediaType; previewWindow = snapshot.previewWindow
        searchPathPrefix = snapshot.searchPathPrefix; searchDateFrom = snapshot.searchDateFrom
        searchDateTo = snapshot.searchDateTo; searchRequireOCR = snapshot.searchRequireOCR
        searchRequirePerson = snapshot.searchRequirePerson
        searchStatus = snapshot.searchStatus; searchDiagnostic = snapshot.searchDiagnostic
        searchResults = snapshot.searchResults; bufferedSearchResults = snapshot.bufferedSearchResults
        searchCoverage = snapshot.searchCoverage; searchTotalCount = snapshot.searchTotalCount
        nextSearchOffset = snapshot.nextSearchOffset; serverNextSearchOffset = snapshot.serverNextSearchOffset
        lastSearchSignature = snapshot.lastSearchSignature
        lastSearchMediaSummary = snapshot.lastSearchMediaSummary
        lastSuccessfulSearchDuration = snapshot.lastSuccessfulSearchDuration
        activePersonClusterId = snapshot.activePersonClusterId
        activePersonSourceId = snapshot.activePersonSourceId
        activeSourceContentId = snapshot.activeSourceContentId
        selectedPersonClusterId = snapshot.selectedPersonClusterId
        selectedExportResults = snapshot.selectedExportResults; exportStatus = snapshot.exportStatus
        searchProgress = nil; searchCancelling = false
        searchNavigationDepth = searchNavigationStack.count
        searchNavigationTitle = searchNavigationStack.last?.returnTitle ?? ""
        page = .search
    }

    private func runAction(
        _ arguments: [String],
        pendingMessage: String,
        successPage: ArchivePage? = nil,
        clearSearchOnSuccess: Bool = false
    ) {
        guard !actionInProgress && !terminating else { return }
        actionInProgress = true
        actionFailed = false; actionMessage = pendingMessage
        runHelper(arguments) { data, error in
            self.actionInProgress = false
            if let data, let response = try? self.decoder().decode(ActionResponse.self, from: data), response.status == "PASS" {
                self.actionMessage = [response.message, response.path].compactMap { $0 }.joined(separator: " · ")
                self.actionFailed = false
                if clearSearchOnSuccess {
                    self.clearSearchForLibraryChange()
                    self.loadSearchMetadata()
                    self.loadPersonClusters()
                }
                if let successPage { self.page = successPage }
                self.loadSnapshot()
            } else if let data, let response = try? self.decoder().decode(ErrorResponse.self, from: data) {
                self.actionMessage = response.error ?? "操作失败"; self.actionFailed = true
            } else {
                self.actionMessage = (error?.isEmpty == false ? error! : "操作失败"); self.actionFailed = true
            }
        }
    }

    func prepareToQuit(_ completion: @escaping (Bool) -> Void) {
        terminating=true
        stopSearchPreparation()
        build151LifecycleEvent("shutdown_begin; reject_actions; stop_polling")
        refreshTimer?.invalidate();refreshTimer=nil
        stopSearchTimer();activeSearchProcess?.terminate();activeSearchProcess=nil
        activeStatusSnapshotProcess?.terminate();activeStatusSnapshotProcess=nil
        snapshotGeneration += 1;snapshotRequestInFlight=false
        // First stop/reap readers, then the existing scheduler handoff, then exit.
        Build151ChildOwner.begin()
        DispatchQueue.global(qos:.utility).async {
            let c=Bundle.main.bundleURL.appendingPathComponent("Contents"),p=Process(),out=Pipe(),err=Pipe()
            p.executableURL=c.appendingPathComponent("Helpers/PipelinePython/system")
            p.arguments=[c.appendingPathComponent("Resources/build151_owned_helper.py").path,c.appendingPathComponent("Resources/build151_lifecycle_cli.py").path,"shutdown"]
            p.environment=phaseCEnvironment();p.standardOutput=out;p.standardError=err
            do {try p.run(); Build151ChildOwner.register(p);let data=out.fileHandleForReading.readDataToEndOfFile();let detail=err.fileHandleForReading.readDataToEndOfFile();p.waitUntilExit(); Build151ChildOwner.finish(p)
                let value=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any]
                DispatchQueue.main.async {let ready=p.terminationStatus==0 && value?["status"] as? String=="PASS";if !ready {self.terminating=false;self.actionFailed=true;self.actionMessage=value?["error"] as? String ?? String(data:detail,encoding:.utf8) ?? "安全退出失败"};completion(ready)}
            } catch {DispatchQueue.main.async {self.terminating=false;self.actionFailed=true;self.actionMessage=error.localizedDescription;completion(false)}}
        }
    }

    func openStageReport(_ stage: PipelineStage) {
        guard let path = stage.reportPaths?["summary"], !path.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    private func updateSearchPageMarker() {
        nextSearchOffset = (
            searchResults.count < bufferedSearchResults.count
            ? searchResults.count
            : serverNextSearchOffset
        )
    }

    func recordSearchCardVisible() {
        guard searching, !searchFirstVisibleRecorded, let started=searchStartedAt else {return}
        searchFirstVisibleRecorded=true
        build151LifecycleEvent("search_first_card_appeared id=\(searchMeasurementID) elapsed=\((Double(DispatchTime.now().uptimeNanoseconds)/1_000_000_000 - started))")
    }

    private func mergeProgressResults(_ event: SearchProgressEvent) {
        guard let incoming = event.resultItems, !incoming.isEmpty else { return }
        if event.ordering == "federated_rank", event.updateMode == "replace" {
            searchResults=incoming;searchTotalCount=event.totalCount ?? incoming.count
            if !searchFirstResultRecorded,let started=searchStartedAt {searchFirstResultRecorded=true;build151LifecycleEvent("search_first_received id=\(searchMeasurementID) elapsed=\((Double(DispatchTime.now().uptimeNanoseconds)/1_000_000_000-started))")}
            searchStatus=event.message;return
        }
        var merged = searchResults
        for item in incoming {
            let key = item.sourceId ?? item.sourceContentId ?? item.id
            if let index = merged.firstIndex(where: { ($0.sourceId ?? $0.sourceContentId ?? $0.id) == key }) {
                merged[index] = item
            } else {
                merged.append(item)
            }
        }
        if !searchFirstResultRecorded, let started = searchStartedAt {
            searchFirstResultRecorded = true
            build151LifecycleEvent("search_first_received id=\(searchMeasurementID) elapsed=\((Double(DispatchTime.now().uptimeNanoseconds)/1_000_000_000 - started))")
        }
        searchResults = merged.sorted {
            let left = $0.finalScore ?? $0.score ?? 0
            let right = $1.finalScore ?? $1.score ?? 0
            return left > right || (left == right && ($0.sourceId ?? $0.sourceContentId ?? $0.id) < ($1.sourceId ?? $1.sourceContentId ?? $1.id))
        }
        if let count = event.totalCount ?? event.currentSourceResultCount { searchTotalCount = max(searchTotalCount, count) }
        if let next = event.nextResultOffset { serverNextSearchOffset = next }
        updateSearchPageMarker()
        let scanned = event.scannedCount.map(String.init) ?? "?"
        let total = event.totalCount.map(String.init) ?? "?"
        searchStatus = "正在搜索 · 已扫描 \(scanned) / \(total) · 找到 \(searchResults.count) 个素材"
    }

    private func showNextBufferedSearchPage() {
        let visibleCount = searchResults.count
        let end = min(visibleCount + 30, bufferedSearchResults.count)
        guard end > visibleCount else { return }
        searchResults.append(
            contentsOf: bufferedSearchResults[visibleCount..<end]
        )
        updateSearchPageMarker()
        searchStatus = "共 \(searchTotalCount) 条匹配结果\(lastSearchMediaSummary) · 当前显示 \(searchResults.count) 条 · 已即时展开缓存结果，未重新搜索"
    }

    private func finishSearchStatus(reused: Bool = false) {
        let elapsed = lastSuccessfulSearchDuration.map {
            " · 用时 \(String(format: "%.1f", $0)) 秒"
        } ?? ""
        searchStatus = searchTotalCount == 0
            ? "没有找到匹配素材"
            : "共 \(searchTotalCount) 条匹配结果\(lastSearchMediaSummary) · 当前显示 \(searchResults.count) 条\(reused ? " · 已即时复用本次结果" : elapsed) · 搜索结果只读，查询历史仅保存在当前素材库"
    }

    func changeSearchIntent() {
        guard !searching else { return }
        searchResults = []; bufferedSearchResults = []
        searchCoverage = nil; searchTotalCount = 0; nextSearchOffset = nil
        serverNextSearchOffset = nil; lastSearchSignature = ""
        activePersonClusterId = ""; activePersonSourceId = ""; activeSourceContentId = ""
        searchProgress = nil; searchDiagnostic = ""; searchLibraryNotices = []; lastSuccessfulSearchDuration = nil
        clearSearchNavigation()
        searchStatus = "搜索目标已切换，请点击搜索获取对应结果"
    }

    func search(loadMore: Bool = false) {
        if loadMore, lastSearchSignature.hasPrefix("person-track:"), !selectedPersonClusterId.isEmpty {
            searchPersonTrackSuggestions(selectedPersonClusterId, loadMore: true)
            return
        }
        if loadMore, !activeSourceContentId.isEmpty {
            browseSourceFrames(activeSourceContentId, loadMore: true)
            return
        }
        if loadMore, !activePersonClusterId.isEmpty {
            searchPersonCluster(activePersonClusterId, loadMore: true, sourceContentId: activePersonSourceId.isEmpty ? nil : activePersonSourceId)
            return
        }
        guard !searching else { return }
        guard !selectedSearchLibraries.isEmpty || snapshot?.searchRuntime.ready == true else {
            searchStatus = "当前素材库尚未通过搜索预检"
            return
        }
        let clean = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { searchStatus = "请输入要搜索的内容"; return }
        guard let advancedArguments = searchAdvancedArguments() else { return }
        let signature = "\(clean)\u{0}\(searchIntent)\u{0}\(mediaType)\u{0}\(previewWindow)\u{0}\(searchPathPrefix)\u{0}\(searchDateFrom)\u{0}\(searchDateTo)\u{0}\(searchRequireOCR)\u{0}\(searchRequirePerson)\u{0}\(selectedSearchLibraries.sorted().joined(separator: "|"))"
        let continuing = loadMore && signature == lastSearchSignature
        if !loadMore { clearSearchNavigation() }
        if continuing && searchResults.count < bufferedSearchResults.count {
            showNextBufferedSearchPage()
            return
        }
        let offset = continuing ? (serverNextSearchOffset ?? 0) : 0
        if continuing && serverNextSearchOffset == nil { return }
        searching = true
        searchCancelling = false
        searchProgress = SearchProgressEvent(
            contract: "media_archive_search_progress_v1",
            stage: "starting", stageIndex: 1, totalStages: 7,
            message: "正在启动本地只读搜索",
            detail: "查询“\(clean)” · 范围：\(mediaType) · 预览：\(previewWindow)",
            completed: nil, total: nil, elapsedSeconds: 0
        )
        startSearchTimer()
        if !continuing {
            searchResults = []; searchTotalCount = 0; nextSearchOffset = nil
            // A new query must not display coverage from the previous media
            // filter while the replacement search is still running.
            searchCoverage = nil
            lastSearchSignature = signature; activePersonClusterId = ""
            activePersonSourceId = ""
            activeSourceContentId = ""
            selectedPersonClusterId = ""; searchDiagnostic = ""
            bufferedSearchResults = []; serverNextSearchOffset = nil
            lastSearchMediaSummary = ""
        }
        searchStatus = continuing
            ? "正在获取下一批结果；已有结果会继续保留…"
            : "正在搜索“\(clean)”；下方会实时显示范围、阶段和耗时…"
        let media = mediaType == "视频" ? "video" : (mediaType == "图片" ? "image" : (mediaType == "音频（人声转写）" ? "audio" : "all"))
        let window = previewWindow == "5 秒" ? "5000" : "10000"
        runSearchHelper([
            "search", "--query", clean, "--search-intent", searchIntent, "--media-type", media,
            "--preview-window-ms", window, "--result-offset", String(offset),
            "--result-limit", "30",
        ] + advancedArguments + selectedSearchLibraries.sorted().flatMap { ["--library-task-path", $0] }, progress: { event in
            self.searchProgress = event
            self.mergeProgressResults(event)
            if let elapsed = event.elapsedSeconds {
                self.searchElapsedSeconds = max(self.searchElapsedSeconds, elapsed)
            }
        }) { data, error in
            defer {
                if self.page == .search && !self.terminating && (!self.semanticPrepared || self.selectedSearchLibraries.contains(where: {self.libraryPreparation[$0] == "准备中"})) {self.requestSearchPreparation()}
            }
            let wasCancelled = self.searchCancelling
            self.searching = false
            self.searchCancelling = false
            self.stopSearchTimer()
            build151LifecycleEvent("search_finished id=\(self.searchMeasurementID) elapsed=\(self.searchElapsedSeconds) cancelled=\(wasCancelled)")
            if wasCancelled {
                self.searchProgress = nil
                self.loadSearchMetadata()
                self.searchStatus = "已取消本次搜索；已有结果未受影响"
                return
            }
            if let data, let envelope = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
               let status = envelope["status"] as? String, status == "INCOMPLETE" || status == "CANCELLED" {
                // Progress items already passed the existing result contract. Keep them,
                // but never cache an incomplete request as a reusable successful search.
                if let partial = try? self.decoder().decode(SearchResponse.self, from: data), let items = partial.resultItems, !items.isEmpty {
                    let existing = Set(self.searchResults.map(\.id))
                    self.searchResults.append(contentsOf: items.filter { !existing.contains($0.id) })
                    self.searchTotalCount = max(self.searchTotalCount, self.searchResults.count)
                }
                self.lastSearchSignature = ""
                self.serverNextSearchOffset = nil
                self.nextSearchOffset = nil
                self.searchProgress = nil
                self.searchStatus = status == "INCOMPLETE" ? "搜索未完成，可重试" : "已取消本次搜索；已有结果未受影响"
                self.searchDiagnostic = (envelope["error_reason"] as? String) ?? ""
                return
            }
            if let data, let response = try? self.decoder().decode(SearchResponse.self, from: data), response.status == "PASS" {
                let incoming = response.resultItems ?? []
                if continuing {
                    let existing = Set(self.bufferedSearchResults.map(\.id))
                    self.bufferedSearchResults.append(
                        contentsOf: incoming.filter { !existing.contains($0.id) }
                    )
                } else {
                    self.bufferedSearchResults = incoming
                    self.searchResults = []
                }
                self.searchCoverage = response.coverage
                self.searchTotalCount = response.resultTotalCount
                    ?? self.bufferedSearchResults.count
                self.serverNextSearchOffset = response.nextResultOffset
                let mediaCounts = response.resultCountByMedia ?? [:]
                self.lastSearchMediaSummary = media == "all" && self.selectedSearchLibraries.isEmpty
                    ? " · 图片 \(mediaCounts["image"] ?? 0) 条，视频 \(mediaCounts["video"] ?? 0) 条"
                    : ""
                self.lastSuccessfulSearchDuration = self.searchElapsedSeconds
                self.showNextBufferedSearchPage()
                self.finishSearchStatus()
                self.searchLibraryNotices = (response.libraryErrors ?? []).map { error in
                    let explanation = error.reason.contains("library_has_no_searchable_published_sources") ? "尚无可搜索内容，已跳过。" : "暂时无法搜索，已跳过；其他素材库不受影响。"
                    return "\(simplifiedContent(error.library))：\(explanation)"
                }
                self.searchDiagnostic = (response.libraryErrors ?? []).map { "\($0.library)：\($0.reason)" }.joined(separator: "\n")
                if !(response.libraryErrors ?? []).isEmpty {
                    self.searchStatus += " · 部分库已跳过，其余库搜索完成"
                }
                self.searchProgress = nil
            } else if let data, let failure = try? self.decoder().decode(ErrorResponse.self, from: data) {
                self.searchStatus = failure.displayMessage.isEmpty ? "搜索失败" : failure.displayMessage
                self.searchDiagnostic = failure.diagnosticText
                self.searchProgress = nil
            } else {
                self.searchStatus = error ?? "搜索失败，请检查本地模型和索引状态"
                self.searchDiagnostic = error ?? ""
                self.searchProgress = nil
            }
        }
    }

    func loadSearchMetadata() {
        let requestedLibrary = libraryGeneration
        runHelper(["search-metadata"]) { data, error in
            guard requestedLibrary == self.libraryGeneration else { return }
            guard let data else {
                self.searchMetadataStatus = error ?? "无法读取搜索历史"
                return
            }
            do {
                let response = try self.decoder().decode(SearchMetadataResponse.self, from: data)
                self.searchHistory = response.history
                self.savedSearches = response.savedSearches
                self.searchMetadataStatus = ""
            } catch {
                self.searchMetadataStatus = "搜索历史解析失败：\(error.localizedDescription)"
            }
        }
    }

    func applySearchMetadata(_ queryText: String, filters: SearchMetadataFilters) {
        query = queryText
        mediaType = filters.mediaType == "video" ? "视频" : (filters.mediaType == "image" ? "图片" : (filters.mediaType == "audio" ? "音频（人声转写）" : "全部"))
        previewWindow = filters.previewWindowMs == 5000 ? "5 秒" : "10 秒"
        searchPathPrefix = filters.pathPrefix ?? ""
        searchRequireOCR = filters.hasOcr ?? false
        searchRequirePerson = filters.hasPerson ?? false
        searchDateFrom = filters.sourceMtimeMin.map(dateText) ?? ""
        searchDateTo = filters.sourceMtimeMax.map(dateText) ?? ""
    }

    private func dateText(_ epoch: Int) -> String {
        let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: Date(timeIntervalSince1970: TimeInterval(epoch)))
    }

    private func searchAdvancedArguments() -> [String]? {
        let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        var arguments: [String] = []
        let prefix = searchPathPrefix.trimmingCharacters(in: .whitespacesAndNewlines)
        if !prefix.isEmpty { arguments += ["--path-prefix", prefix] }
        if !searchDateFrom.isEmpty {
            guard let date = formatter.date(from: searchDateFrom) else {
                searchStatus = "开始日期请使用 YYYY-MM-DD"; return nil
            }
            arguments += ["--source-mtime-min", String(Int(date.timeIntervalSince1970))]
        }
        if !searchDateTo.isEmpty {
            guard let date = formatter.date(from: searchDateTo),
                  let end = Calendar.current.date(byAdding: .day, value: 1, to: date) else {
                searchStatus = "结束日期请使用 YYYY-MM-DD"; return nil
            }
            arguments += ["--source-mtime-max", String(Int(end.timeIntervalSince1970) - 1)]
        }
        if searchRequireOCR { arguments.append("--has-ocr") }
        if searchRequirePerson { arguments.append("--has-person") }
        return arguments
    }

    func saveCurrentSearch() {
        let cleanQuery = query.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanName = savedSearchName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanQuery.isEmpty, !cleanName.isEmpty else {
            searchMetadataStatus = "请填写保存名称并输入搜索词"
            return
        }
        let media = mediaType == "视频" ? "video" : (mediaType == "图片" ? "image" : (mediaType == "音频（人声转写）" ? "audio" : "all"))
        let window = previewWindow == "5 秒" ? "5000" : "10000"
        guard let advancedArguments = searchAdvancedArguments() else { return }
        runHelper([
            "save-search", "--name", cleanName, "--query", cleanQuery,
            "--media-type", media, "--preview-window-ms", window,
        ] + advancedArguments) { data, error in
            if data != nil {
                self.searchMetadataStatus = "已保存到当前素材库"
                self.savedSearchName = ""
                self.loadSearchMetadata()
            } else {
                self.searchMetadataStatus = error ?? "保存搜索失败"
            }
        }
    }

    func saveResultAnnotation(
        _ result: SearchResult,
        tags: String,
        note: String,
        favorite: Bool,
        rating: Int,
        ignored: Bool,
        completion: ((Bool, String) -> Void)? = nil
    ) {
        guard let sourceId = result.sourceContentId, !sourceId.isEmpty else {
            let message = "当前结果缺少来源标识，无法保存备注"
            searchMetadataStatus = message
            completion?(false, message)
            return
        }
        runHelper([
            "annotate-source", "--source-content-id", sourceId,
            "--tags", tags, "--note", note,
            "--favorite", favorite ? "true" : "false",
            "--rating", String(rating),
            "--ignored", ignored ? "true" : "false",
        ]) { data, error in
            let message = data != nil
                ? "素材标签、备注和星标已保存；模型结果未修改"
                : (error ?? "保存素材备注失败")
            self.searchMetadataStatus = message
            completion?(data != nil, message)
            if data != nil, self.page == .favorites {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                    self.loadFavorites()
                }
            }
        }
    }

    func loadFavorites() {
        guard !favoriteLoading else { return }
        favoriteLoading = true
        favoriteStatus = "正在读取当前素材库的本地收藏…"
        runHelper(["favorites", "--result-offset", "0", "--result-limit", "500"]) { data, error in
            self.favoriteLoading = false
            guard let data else {
                self.favoriteStatus = error ?? "无法读取我的收藏"
                return
            }
            do {
                let response = try self.decoder().decode(SearchResponse.self, from: data)
                self.favoriteResults = response.resultItems ?? []
                self.favoriteStatus = self.favoriteResults.isEmpty
                    ? "当前素材库还没有收藏；可在搜索结果中勾选“收藏”并保存。"
                    : "当前素材库共有 \(response.resultTotalCount ?? self.favoriteResults.count) 项收藏。"
            } catch {
                self.favoriteStatus = "收藏数据解析失败：\(error.localizedDescription)"
            }
        }
    }

    func isSelectedForExport(_ result: SearchResult) -> Bool {
        selectedExportResults[result.exportSelectionId] != nil
    }

    func setSelectedForExport(_ result: SearchResult, selected: Bool) {
        if selected { selectedExportResults[result.exportSelectionId] = result }
        else { selectedExportResults.removeValue(forKey: result.exportSelectionId) }
    }

    func selectForExport(_ results: [SearchResult]) {
        for result in results { selectedExportResults[result.exportSelectionId] = result }
    }

    func clearExportSelection() {
        selectedExportResults.removeAll()
        exportStatus = ""
    }

    private var orderedExportResults: [SearchResult] {
        selectedExportResults.values.sorted {
            let left = ($0.sourceRelativePath ?? "", $0.timecode ?? "")
            let right = ($1.sourceRelativePath ?? "", $1.timecode ?? "")
            return left < right
        }
    }

    private func exportText(for result: SearchResult, index: Int) -> String {
        let people = (result.personClusters ?? []).compactMap { $0.displayName }.filter { !$0.isEmpty }.joined(separator: "、")
        let tags = (result.userAnnotation?.tags ?? []).joined(separator: "、")
        let lines = [
            "\(index). \(URL(fileURLWithPath: result.sourceRelativePath ?? "素材").lastPathComponent)",
            "位置：\(result.sourceRelativePath ?? "--")",
            "类型：\(result.mediaType ?? "--")　时间点：\(result.timecode ?? "--")",
            "人物：\(people.isEmpty ? "--" : people)",
            "标签：\(tags.isEmpty ? "--" : tags)",
            "备注：\(result.userAnnotation?.note ?? "--")",
            "描述/命中证据：\(result.textPreview ?? "--")",
            "命中通道：\((result.relevanceReasons ?? []).joined(separator: "、"))",
        ]
        return lines.joined(separator: "\n")
    }

    private func csvField(_ value: String) -> String {
        "\"" + value.replacingOccurrences(of: "\"", with: "\"\"") + "\""
    }

    func exportSelectedCSV() {
        let results = orderedExportResults
        guard !results.isEmpty else { exportStatus = "请先勾选需要导出的画面或素材"; return }
        let panel = NSSavePanel(); panel.nameFieldStringValue = "本地素材收藏与选片.csv"
        panel.allowedContentTypes = [.commaSeparatedText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let header = ["序号", "文件名", "相对路径", "素材类型", "时间点", "人物", "用户标签", "用户备注", "描述或命中证据", "命中通道"]
        var rows = [header.map(csvField).joined(separator: ",")]
        for (index, result) in results.enumerated() {
            let people = (result.personClusters ?? []).compactMap { $0.displayName }.filter { !$0.isEmpty }.joined(separator: "、")
            let values = [
                String(index + 1), URL(fileURLWithPath: result.sourceRelativePath ?? "素材").lastPathComponent,
                result.sourceRelativePath ?? "", result.mediaType ?? "", result.timecode ?? "",
                people, (result.userAnnotation?.tags ?? []).joined(separator: "、"),
                result.userAnnotation?.note ?? "", result.textPreview ?? "",
                (result.relevanceReasons ?? []).joined(separator: "、"),
            ]
            rows.append(values.map(csvField).joined(separator: ","))
        }
        do {
            try ("\u{FEFF}" + rows.joined(separator: "\n")).write(to: url, atomically: true, encoding: .utf8)
            exportStatus = "已导出 \(results.count) 项：\(url.path)"
        } catch { exportStatus = "CSV 导出失败：\(error.localizedDescription)" }
    }

    func exportSelectedPDF() {
        let results = orderedExportResults
        guard !results.isEmpty else { exportStatus = "请先勾选需要导出的画面或素材"; return }
        let panel = NSSavePanel(); panel.nameFieldStringValue = "本地素材收藏与选片.pdf"
        panel.allowedContentTypes = [.pdf]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let title = "本地素材收藏与选片\n导出时间：\(DateFormatter.localizedString(from: Date(), dateStyle: .medium, timeStyle: .short))\n共 \(results.count) 项\n\n"
        let body = results.enumerated().map { exportText(for: $0.element, index: $0.offset + 1) }.joined(separator: "\n\n")
        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 720, height: max(900, CGFloat((title + body).count) * 0.72)))
        textView.string = title + body
        textView.font = NSFont(name: "PingFangSC-Regular", size: 12) ?? NSFont.systemFont(ofSize: 12)
        textView.textContainerInset = NSSize(width: 36, height: 36)
        textView.isEditable = false
        do {
            try textView.dataWithPDF(inside: textView.bounds).write(to: url)
            exportStatus = "已导出 \(results.count) 项：\(url.path)"
        } catch { exportStatus = "PDF 导出失败：\(error.localizedDescription)" }
    }

    func loadPersonClusters() {
        guard !personClusterLoading else { return }
        personClusterLoading = true
        let requestedLibrary = libraryGeneration
        runHelper(["person-clusters", "--result-offset", "0", "--result-limit", "100"]) { data, error in
            guard requestedLibrary == self.libraryGeneration else { return }
            self.personClusterLoading = false
            guard let data else {
                self.personCapabilityNote = error ?? "无法读取同一人物分组"
                return
            }
            do {
                let response = try self.decoder().decode(PersonClusterCatalogResponse.self, from: data)
                self.personClusterCatalog = response.items
                self.personCapabilityNote = response.capabilityNote ?? ""
            } catch {
                self.personCapabilityNote = "同一人物分组解析失败：\(error.localizedDescription)"
            }
        }
    }

    func preparePersonEditor() {
        guard let person = personClusterCatalog.first(where: { $0.personClusterId == selectedPersonClusterId }) else {
            personDisplayName = ""; personTags = ""; personMergeTargetId = ""; return
        }
        personDisplayName = person.displayName.hasPrefix("匿名人物 ") ? "" : person.displayName
        personTags = (person.tags ?? []).joined(separator: "，")
        if personMergeTargetId == selectedPersonClusterId { personMergeTargetId = "" }
    }

    func savePersonName() {
        let name = personDisplayName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !selectedPersonClusterId.isEmpty, !name.isEmpty else {
            personEditStatus = "请先选择人物组并填写名称"; return
        }
        runHelper([
            "person-name", "--person-id", selectedPersonClusterId,
            "--display-name", name, "--tags", personTags,
        ]) { data, error in
            if data != nil {
                self.personEditStatus = "名称与标签已保存在本机"
                self.loadPersonClusters()
            } else { self.personEditStatus = error ?? "保存失败" }
        }
    }

    func mergeSelectedPerson() {
        guard !selectedPersonClusterId.isEmpty, !personMergeTargetId.isEmpty else {
            personEditStatus = "请选择要归入的目标人物"; return
        }
        runHelper([
            "person-merge", "--source-person-id", selectedPersonClusterId,
            "--target-person-id", personMergeTargetId,
        ]) { data, error in
            if data != nil {
                self.selectedPersonClusterId = self.personMergeTargetId
                self.personMergeTargetId = ""
                self.personEditStatus = "已合并为同一个本地人物；可随时拆分"
                self.loadPersonClusters()
            } else { self.personEditStatus = error ?? "合并失败" }
        }
    }

    func detachSelectedPerson() {
        guard !selectedPersonClusterId.isEmpty else { return }
        runHelper(["person-detach", "--person-id", selectedPersonClusterId]) { data, error in
            if data != nil {
                self.personEditStatus = "已取消人工合并，恢复为独立机器人物组"
                self.selectedPersonClusterId = ""
                self.loadPersonClusters()
            } else { self.personEditStatus = error ?? "拆分失败" }
        }
    }

    func searchPersonCluster(_ clusterId: String, loadMore: Bool = false, sourceContentId: String? = nil) {
        let cleanId = clusterId.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanId.isEmpty else { return }
        let continuing = loadMore && activePersonClusterId == cleanId
        let offset = continuing ? (nextSearchOffset ?? 0) : 0
        if continuing && nextSearchOffset == nil { return }
        if !continuing {
            pushSearchNavigation(title: sourceContentId == nil ? "原始搜索结果" : "人物素材列表")
            page = .search
        }
        searching = true
        if !continuing {
            searchResults = []; searchTotalCount = 0; nextSearchOffset = nil
            searchCoverage = nil; searchDiagnostic = ""; activePersonClusterId = cleanId
            activePersonSourceId = sourceContentId ?? ""
            activeSourceContentId = ""
            selectedPersonClusterId = cleanId
            lastSearchSignature = "person:\(cleanId)"
        }
        searchStatus = continuing ? "正在加载更多结果…" : (sourceContentId == nil ? "正在按素材归并待确认人物组…" : "正在展开这个素材中的人物画面…")
        let media = mediaType == "视频" ? "video" : (mediaType == "图片" ? "image" : (mediaType == "音频（人声转写）" ? "audio" : "all"))
        let window = previewWindow == "5 秒" ? "5000" : "10000"
        var arguments = [
            "person-cluster", "--cluster-id", cleanId, "--media-type", media,
            "--preview-window-ms", window, "--result-offset", String(offset),
            "--result-limit", "30",
        ]
        if let sourceContentId, !sourceContentId.isEmpty {
            arguments += ["--source-content-id", sourceContentId]
        }
        runHelper(arguments) { data, error in
            self.searching = false
            if let data, let response = try? self.decoder().decode(SearchResponse.self, from: data),
               response.status == "PASS" {
                let incoming = response.resultItems ?? []
                if continuing {
                    let existing = Set(self.searchResults.map(\.id))
                    self.searchResults.append(contentsOf: incoming.filter { !existing.contains($0.id) })
                } else {
                    self.searchResults = incoming
                }
                self.searchTotalCount = response.resultTotalCount ?? self.searchResults.count
                self.nextSearchOffset = response.nextResultOffset
                self.searchStatus = self.searchTotalCount == 0
                    ? "没有找到可确认的同一人物画面"
                    : (sourceContentId == nil
                        ? "待确认人物组涉及 \(self.searchTotalCount) 个素材 · 当前显示 \(self.searchResults.count) 个；可继续展开素材内画面"
                        : "这个素材中共有 \(self.searchTotalCount) 个相关画面 · 当前显示 \(self.searchResults.count) 个")
            } else if let data, let failure = try? self.decoder().decode(ErrorResponse.self, from: data) {
                self.searchStatus = failure.displayMessage.isEmpty ? "读取同一人物失败" : failure.displayMessage
                self.searchDiagnostic = failure.diagnosticText
            } else {
                self.searchStatus = error ?? "读取同一人物失败"
                self.searchDiagnostic = error ?? ""
            }
        }
    }

    func searchPersonTrackSuggestions(_ clusterId: String, loadMore: Bool = false) {
        let cleanId = clusterId.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanId.isEmpty else { return }
        let signature = "person-track:\(cleanId)"
        let continuing = loadMore && lastSearchSignature == signature
        let offset = continuing ? (nextSearchOffset ?? 0) : 0
        if continuing && nextSearchOffset == nil { return }
        if !continuing {
            pushSearchNavigation(title: "人物搜索结果")
            page = .search
            searchResults = []; searchTotalCount = 0; nextSearchOffset = nil
            searchCoverage = nil; searchDiagnostic = ""
            activePersonClusterId = ""; activePersonSourceId = ""; activeSourceContentId = ""
            selectedPersonClusterId = cleanId
            lastSearchSignature = signature
        }
        searching = true
        searchStatus = continuing ? "正在加载更多人物轨迹候选…" : "正在读取同一视频中的侧脸和背影候选…"
        let media = mediaType == "图片" ? "image" : (mediaType == "视频" ? "video" : "all")
        let window = previewWindow == "5 秒" ? "5000" : "10000"
        runHelper([
            "person-track-suggestions", "--cluster-id", cleanId,
            "--media-type", media, "--preview-window-ms", window,
            "--result-offset", String(offset), "--result-limit", "30",
        ]) { data, error in
            self.searching = false
            if let data, let response = try? self.decoder().decode(SearchResponse.self, from: data),
               response.status == "PASS" {
                let incoming = response.resultItems ?? []
                if continuing {
                    let existing = Set(self.searchResults.map(\.id))
                    self.searchResults.append(contentsOf: incoming.filter { !existing.contains($0.id) })
                } else {
                    self.searchResults = incoming
                }
                self.searchTotalCount = response.resultTotalCount ?? self.searchResults.count
                self.nextSearchOffset = response.nextResultOffset
                self.searchStatus = self.searchTotalCount == 0
                    ? "没有找到足够可靠的侧脸或背影候选"
                    : "找到 \(self.searchTotalCount) 个待人工确认候选 · 当前显示 \(self.searchResults.count) 个；确认前不会加入人物"
            } else if let data, let failure = try? self.decoder().decode(ErrorResponse.self, from: data) {
                self.searchStatus = failure.displayMessage.isEmpty ? "读取人物轨迹候选失败" : failure.displayMessage
                self.searchDiagnostic = failure.diagnosticText
            } else {
                self.searchStatus = error ?? "读取人物轨迹候选失败"
                self.searchDiagnostic = error ?? ""
            }
        }
    }

    func browseSourceFrames(_ sourceContentId: String, loadMore: Bool = false) {
        let cleanId = sourceContentId.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanId.isEmpty, !searching else { return }
        let continuing = loadMore && activeSourceContentId == cleanId
        let offset = continuing ? (nextSearchOffset ?? 0) : 0
        if continuing && nextSearchOffset == nil { return }
        searching = true
        if !continuing {
            pushSearchNavigation(title: activePersonClusterId.isEmpty ? "原始搜索结果" : "人物搜索结果")
            page = .search
            searchResults = []; searchTotalCount = 0; nextSearchOffset = nil
            searchCoverage = nil; searchDiagnostic = ""
            activeSourceContentId = cleanId
            activePersonClusterId = ""; activePersonSourceId = ""
            lastSearchSignature = "source-frames:\(cleanId)"
        }
        searchStatus = continuing ? "正在加载该视频的更多画面…" : "正在读取该视频的全部索引画面…"
        let window = previewWindow == "5 秒" ? "5000" : "10000"
        runHelper([
            "source-frames", "--source-content-id", cleanId,
            "--preview-window-ms", window,
            "--result-offset", String(offset), "--result-limit", "60",
        ]) { data, error in
            self.searching = false
            if let data, let response = try? self.decoder().decode(SearchResponse.self, from: data),
               response.status == "PASS" {
                let incoming = response.resultItems ?? []
                if continuing {
                    let existing = Set(self.searchResults.map(\.id))
                    self.searchResults.append(contentsOf: incoming.filter { !existing.contains($0.id) })
                } else {
                    self.searchResults = incoming
                }
                self.searchTotalCount = response.resultTotalCount ?? self.searchResults.count
                self.nextSearchOffset = response.nextResultOffset
                self.searchStatus = self.searchTotalCount == 0
                    ? "这个视频没有可显示的索引画面"
                    : "该视频共有 \(self.searchTotalCount) 个索引画面 · 当前显示 \(self.searchResults.count) 个 · 已按时间排序"
            } else if let data, let failure = try? self.decoder().decode(ErrorResponse.self, from: data) {
                self.searchStatus = failure.displayMessage.isEmpty ? "读取视频画面失败" : failure.displayMessage
                self.searchDiagnostic = failure.diagnosticText
            } else {
                self.searchStatus = error ?? "读取视频画面失败"
                self.searchDiagnostic = error ?? ""
            }
        }
    }

    func createPerson(
        from result: SearchResult, name: String, tags: String,
        completion: @escaping (Bool, String) -> Void
    ) {
        guard let visualId = result.visualUnitId, !visualId.isEmpty,
              let sourceId = result.sourceContentId, !sourceId.isEmpty else {
            completion(false, "当前结果缺少可关联的画面编号"); return
        }
        runHelper([
            "person-create", "--display-name", name, "--tags", tags,
            "--visual-unit-id", visualId, "--source-content-id", sourceId,
        ]) { data, error in
            if data != nil {
                self.loadPersonClusters()
                completion(true, "已新建人物并加入当前画面")
            } else { completion(false, error ?? "新建人物失败") }
        }
    }

    func addResult(_ result: SearchResult, toPerson personId: String,
                   completion: @escaping (Bool, String) -> Void) {
        guard let visualId = result.visualUnitId, !visualId.isEmpty,
              let sourceId = result.sourceContentId, !sourceId.isEmpty else {
            completion(false, "当前结果缺少可关联的画面编号"); return
        }
        runHelper([
            "person-add-visual", "--person-id", personId,
            "--visual-unit-id", visualId, "--source-content-id", sourceId,
        ]) { data, error in
            if data != nil {
                self.loadPersonClusters()
                completion(true, "当前画面已加入所选人物")
            } else { completion(false, error ?? "加入人物失败") }
        }
    }

    func removeResult(_ result: SearchResult, fromPerson personId: String,
                      completion: @escaping (Bool, String) -> Void) {
        guard let visualId = result.visualUnitId, !visualId.isEmpty else {
            completion(false, "当前结果缺少可关联的画面编号"); return
        }
        runHelper([
            "person-remove-visual", "--person-id", personId,
            "--visual-unit-id", visualId,
        ]) { data, error in
            if data != nil {
                self.loadPersonClusters()
                completion(true, "已移除人工人物关联")
            } else { completion(false, error ?? "移除关联失败") }
        }
    }

    func reveal(_ result: SearchResult) {
        guard let path = result.sourcePath, !path.isEmpty, FileManager.default.fileExists(atPath:path) else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func loadVideoContentTimeline(sourceId: String, expectedRevision: String? = nil) {
        let cleanId = sourceId.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanId.isEmpty else { return }
        videoContentGeneration += 1
        let generation = videoContentGeneration; let library = libraryGeneration
        videoContentExpectedRevision = expectedRevision
        videoContentLoading = true
        videoContentError = ""
        videoContentTimeline = nil
        videoContentSourceId = cleanId
        videoContentSheetPresented = true
        var arguments = ["video-content-timeline", "--source-id", cleanId]
        if let revision = expectedRevision { arguments += ["--expected-revision", revision] }
        runHelper(arguments) { data, error in
            guard generation == self.videoContentGeneration && library == self.libraryGeneration else { return }
            self.videoContentLoading = false
            guard let data else {
                self.videoContentError = error ?? "无法读取视频内容"
                return
            }
            do {
                let timeline = try self.decoder().decode(VideoContentTimeline.self, from: data)
                guard timeline.status == "PASS" else {
                    self.videoContentError = "无法读取视频内容"
                    return
                }
                guard timeline.contract == VideoContentTimeline.contractV1 else {
                    self.videoContentError = "不支持的视频内容数据版本：\(timeline.contract)"
                    return
                }
                self.videoContentTimeline = timeline
            } catch {
                let bridgeError = try? self.decoder().decode(ErrorResponse.self, from: data)
                if let message = bridgeError?.displayMessage, !message.isEmpty {
                    self.videoContentError = message
                } else {
                    self.videoContentError = error.localizedDescription
                }
            }
        }
    }

    func openTimelineVideo(_ timeline: VideoContentTimeline, at milliseconds: Int?) {
        guard timeline.canOpenOriginal == true || timeline.sourceOnline else { return }
        openVideo(path: timeline.sourcePath, milliseconds: milliseconds)
    }

    func open(_ result: SearchResult) {
        guard let path = result.sourcePath, !path.isEmpty, FileManager.default.fileExists(atPath:path) else { return }
        openVideo(path: path, milliseconds: result.mediaType == "video" ? result.previewSegmentStartMs : nil)
    }

    func openSearchMoment(_ result: SearchResult, at milliseconds: Int?) {
        guard let path=result.sourcePath else { return }
        openVideo(path:path,milliseconds:result.mediaType == "video" ? milliseconds:nil)
    }

    private func openVideo(path: String, milliseconds: Int?) {
        guard FileManager.default.fileExists(atPath:path) else { return }
        let url = URL(fileURLWithPath: path)
        if let milliseconds {
            videoPreviewControllers.removeAll { !($0.window?.isVisible ?? false) }
            let item = AVPlayerItem(url: url)
            let player = AVPlayer(playerItem: item)
            let playerView = AVPlayerView(frame: NSRect(x: 0, y: 0, width: 960, height: 600))
            playerView.player = player
            playerView.controlsStyle = .floating
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 960, height: 600),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered, defer: false
            )
            window.title = URL(fileURLWithPath: path).lastPathComponent
            window.contentView = playerView
            window.center()
            let controller = VideoPreviewWindowController(window: window)
            controller.player = player
            videoPreviewControllers.append(controller)
            controller.showWindow(nil)
            NSApp.activate(ignoringOtherApps: true)
            let target = CMTime(
                seconds: Double(milliseconds) / 1000.0,
                preferredTimescale: 1000
            )
            // A mechanical disk may need several seconds before AVFoundation
            // can seek.  Starting playback immediately races that seek and can
            // leave the player at zero.  Wait for readiness, finish the seek,
            // and only then play.
            controller.readinessObservation = item.observe(\.status, options: [.initial, .new]) {
                [weak controller, weak player] observedItem, _ in
                guard observedItem.status == .readyToPlay else { return }
                controller?.readinessObservation?.invalidate()
                controller?.readinessObservation = nil
                observedItem.seek(
                    to: target,
                    toleranceBefore: CMTime(seconds: 0.1, preferredTimescale: 1000),
                    toleranceAfter: CMTime(seconds: 0.1, preferredTimescale: 1000)
                ) { finished in
                    guard finished else { return }
                    DispatchQueue.main.async { player?.play() }
                }
            }
        } else { NSWorkspace.shared.open(url) }
    }

    func previewTimelapseFrame(_ frame: TimelapseFrame) {
        guard let path = frame.previewPath, !path.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func openTimelapseFolder(_ group: TimelapseGroup) {
        if let folder = group.sourceFolder, !folder.isEmpty {
            NSWorkspace.shared.open(URL(fileURLWithPath: folder, isDirectory: true))
            return
        }
        if let path = group.frames.compactMap({ $0.sourcePath }).first, !path.isEmpty {
            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
        }
    }

    func revealDuplicate(_ member: DuplicateMember) {
        guard !member.absolutePath.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting([
            URL(fileURLWithPath: member.absolutePath)
        ])
    }

    func openDuplicateFolder(_ member: DuplicateMember) {
        guard !member.folderPath.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: member.folderPath, isDirectory: true))
    }
}


// Result metadata also identifies current-library results. Only foreign/unknown
// contexts need the cross-library guard; normalization is lexical, without I/O.
func resultRequiresLibrarySwitch(_ resultPath: String?, activePaths: [String]) -> Bool {
    guard let resultPath else { return false }
    guard resultPath.hasPrefix("/"), !resultPath.isEmpty, activePaths.count == 1,
          activePaths[0].hasPrefix("/") else { return true }
    return (resultPath as NSString).standardizingPath != (activePaths[0] as NSString).standardizingPath
}

private struct ResponsiveWidthKey: EnvironmentKey { static let defaultValue: CGFloat = 1400 }
private extension EnvironmentValues {
    var responsiveWidth: CGFloat { get { self[ResponsiveWidthKey.self] } set { self[ResponsiveWidthKey.self] = newValue } }
}
// Layout changes retain the same child identities; never trigger model actions.
private struct ResponsiveRow<Content: View>: View {
    @Environment(\.responsiveWidth) private var width
    var alignment: VerticalAlignment = .center
    var spacing: CGFloat = 12
    @ViewBuilder var content: () -> Content
    var body: some View {
        if #available(macOS 13.0, *) {
            let layout = width < 900 ? AnyLayout(VStackLayout(alignment: .leading, spacing: spacing)) : AnyLayout(HStackLayout(alignment: alignment, spacing: spacing))
            layout { content() }
        } else { VStack(alignment: .leading, spacing: spacing) { content() } }
    }
}

struct Panel<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }
    var body: some View {
        content.padding(20).background(Color(nsColor: .controlBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 13))
            .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.black.opacity(0.07)))
    }
}

struct PrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 13, weight: .semibold)).foregroundStyle(Color(nsColor: .controlBackgroundColor))
            .padding(.horizontal, 18).padding(.vertical, 9)
            .background(RoundedRectangle(cornerRadius: 8).fill(archiveBlue.opacity(configuration.isPressed ? 0.76 : 1.0)))
    }
}

struct PageHeader: View {
    let title: String; let subtitle: String
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 32, weight: .bold))
            Text(subtitle).foregroundStyle(archiveMuted)
        }
    }
}

struct PersonEvidencePreview: View {
    let path: String
    let bbox: [Double]?
    // Evidence geometry remains durable in the backend.  Normal browsing is
    // deliberately clean; a developer can opt in with this private default.
    private var developerOverlaysEnabled: Bool {
        UserDefaults.standard.bool(forKey: "developerEvidenceOverlays")
    }
    var body: some View {
        GeometryReader { geometry in
            if let image = NSImage(contentsOfFile: path) {
                let scale = min(geometry.size.width / max(1, image.size.width), geometry.size.height / max(1, image.size.height))
                let width = image.size.width * scale
                let height = image.size.height * scale
                ZStack {
                    Image(nsImage: image).resizable().scaledToFit()
                    if developerOverlaysEnabled, let box = bbox, box.count == 4 {
                        Rectangle().stroke(Color.orange, lineWidth: 3)
                            .frame(width: max(0, box[2]-box[0]) * width, height: max(0, box[3]-box[1]) * height)
                            .position(x: (geometry.size.width-width)/2 + (box[0]+box[2])/2*width,
                                      y: (geometry.size.height-height)/2 + (box[1]+box[3])/2*height)
                    }
                }
            }
        }
    }
}

struct BrandIcon: View {
    let size: CGFloat
    var body: some View {
        Group {
            if let path = Bundle.main.path(forResource: "app_icon_1024", ofType: "png"),
               let image = NSImage(contentsOfFile: path) {
                Image(nsImage: image).resizable().scaledToFit()
            } else {
                ZStack { RoundedRectangle(cornerRadius: size * 0.2).fill(archiveBlue); Image(systemName: "folder.badge.magnifyingglass").foregroundStyle(.white) }
            }
        }.frame(width: size, height: size).clipShape(RoundedRectangle(cornerRadius: size * 0.22))
    }
}

struct Sidebar: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                BrandIcon(size: 48)
                VStack(alignment: .leading) { Text(bundledAppName).font(.title3.bold()); Text("本地素材整理与搜索").font(.caption).foregroundStyle(archiveMuted) }
            }.padding(.bottom, 22)
            ForEach(ArchivePage.allCases) { page in
                Button { model.page = page } label: {
                    HStack { Image(systemName: page.icon).frame(width: 24); Text(page.rawValue); Spacer() }
                        .padding(.vertical, 11).padding(.horizontal, 12)
                        .background(model.page == page ? Color.blue.opacity(0.12) : Color.clear)
                        .foregroundStyle(model.page == page ? archiveBlue : Color.primary)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                }.buttonStyle(.plain)
            }
            Spacer()
            if let state = model.snapshot {
                if let storage = state.overview.storage {
                    Label("可用 \(formatBytes(storage.free))", systemImage: "internaldrive").font(.caption).foregroundStyle(archiveMuted)
                    ProgressView(value: Double(storage.used), total: Double(storage.total)).tint(archiveBlue)
                }
                let libraryReady = state.configurationState == "configured"
                Label(libraryReady ? (state.database.integrityCheck == "ok" ? "中心数据库正常" : "数据库需要检查") : "尚未创建素材库", systemImage: libraryReady ? "checkmark.shield" : "tray")
                    .font(.caption).foregroundStyle(libraryReady && state.database.integrityCheck == "ok" ? archiveGreen : archiveMuted)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text("© 2026 Horizon-94").font(.caption2).foregroundStyle(archiveMuted)
                Text("版本 \(bundledAppVersion)").font(.caption2).foregroundStyle(archiveMuted)
                Text("构建 \(bundledBuildDate)").font(.system(size: 9)).foregroundStyle(archiveMuted)
            }
        }.padding(24).frame(width: 250).background(Color(nsColor: .controlBackgroundColor).opacity(0.78))
    }
}

struct MetricCard: View {
    let title: String; let value: String; var tint: Color = .primary; var icon: String? = nil
    var body: some View {
        Panel { VStack(alignment: .leading, spacing: 8) {
            HStack { if let icon { Image(systemName: icon).foregroundStyle(archiveBlue) }; Text(title).font(.caption).foregroundStyle(archiveMuted) }
            Text(value).font(.title2.bold()).foregroundStyle(tint)
        }.frame(maxWidth: .infinity, alignment: .leading) }
    }
}

struct NewTaskPage: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "新建扫描任务", subtitle: "选择素材文件夹和处理方式。原始图片与视频始终只读。")
            HStack(alignment: .top, spacing: 20) {
                VStack(spacing: 16) {
                    Panel { VStack(alignment: .leading, spacing: 18) {
                        FormRow(number: "1", title: "整理模式") {
                            Picker("", selection: $model.taskMode) {
                                ForEach(model.taskModes, id: \.self) { mode in
                                    Text(mode).tag(mode)
                                }
                            }.labelsHidden().frame(width: 230)
                        }
                        if model.taskMode == "第一次完整整理" {
                            FormRow(number: "2", title: "素材文件夹") {
                                HStack { TextField("选择要整理的本地文件夹", text: $model.sourceFolder).textFieldStyle(.roundedBorder); Button("浏览…") { model.chooseSourceFolder() } }
                            }
                            FormRow(number: "3", title: "索引保存位置") {
                                HStack { TextField("选择派生文件、数据库和报告的保存位置", text: $model.libraryFolder).textFieldStyle(.roundedBorder); Button("浏览…") { model.chooseLibraryFolder() } }
                            }
                            if !model.libraryFolder.isEmpty {
                                Text("软件会在这里创建 tasks/时间_任务名，用它隔离数据库、日志、阶段产物和断点；原始素材不放入其中。")
                                    .font(.caption).foregroundStyle(archiveMuted).padding(.leading, 154)
                            }
                            FormRow(number: "4", title: "任务名称") { TextField("任务名称", text: $model.taskName).textFieldStyle(.roundedBorder) }
                        } else {
                            FormRow(number: "2", title: "已有素材库") {
                                Picker("", selection: $model.selectedExistingTaskPath) {
                                    ForEach(model.libraryCatalog) { library in
                                        Text(library.displayName).tag(library.taskPath)
                                    }
                                }.labelsHidden().frame(maxWidth: .infinity)
                            }
                            if let library = model.snapshot?.existingLibraries.first(where: { $0.taskPath == model.selectedExistingTaskPath }) {
                                FormRow(number: "3", title: "原素材位置") { Text(library.sourceRoot).lineLimit(2).textSelection(.enabled) }
                                FormRow(number: "4", title: "数据库状态") { Text("\(library.status)｜图片 \(library.imageCount)｜视频 \(library.videoCount)") }
                            }
                        }
                    } }
                    Panel { VStack(alignment: .leading, spacing: 12) {
                        Label("安全边界", systemImage: "lock.shield.fill").font(.headline).foregroundStyle(archiveBlue)
                        Text(model.taskModeExplanation)
                            .font(.subheadline).foregroundStyle(archiveBlue)
                        Text("任一阶段失败都会立即停止并保留断点；不会修改、移动或删除原始素材。")
                            .font(.subheadline).foregroundStyle(archiveMuted)
                        HStack { Spacer(); Button(model.actionInProgress ? "正在启动…" : model.taskActionTitle) { model.startTask() }.buttonStyle(PrimaryButtonStyle()).disabled(model.actionInProgress || (model.taskMode != "第一次完整整理" && model.selectedExistingTaskPath.isEmpty)) }
                    } }.background(Color.blue.opacity(0.025))
                }.frame(maxWidth: .infinity)
                VStack(spacing: 16) {
                    if let state = model.snapshot {
                        Panel { VStack(alignment: .leading, spacing: 13) {
                            Label("本机能力", systemImage: "cpu").font(.headline)
                            SummaryRow("芯片", state.hardware.chip)
                            SummaryRow("CPU", "\(state.hardware.cpuCoresTotal) 核")
                            SummaryRow("GPU", state.hardware.gpuCores.map { "\($0) 核" } ?? "系统未公开")
                            SummaryRow("统一内存", state.hardware.unifiedMemoryGb.map { String(format: "%.0f GB", $0) } ?? "系统未公开")
                            Divider(); Text("保守推荐模型并发 \(state.hardware.recommendation.modelWorkers) 路 · 本机估算上限 \(state.hardware.recommendation.estimatedMaxModelWorkers) 路").font(.caption).foregroundStyle(archiveBlue)
                            Text("抽帧推荐 \(state.hardware.recommendation.frameExtractWorkers) 路；实际运行遇到内存压力会自动降低。").font(.caption2).foregroundStyle(archiveMuted)
                            Button("查看并调整设置") { model.page = .settings }.buttonStyle(.link)
                        } }
                        Panel { VStack(alignment: .leading, spacing: 11) {
                            Label("当前系统", systemImage: "checkmark.circle.fill").font(.headline).foregroundStyle(archiveGreen)
                            SummaryRow("素材库", state.configurationState == "configured" ? "已连接" : "尚未创建")
                            SummaryRow("搜索入口", state.searchRuntime.ready ? "可用" : "等待首次整理")
                            SummaryRow("默认方案", state.hasSavedProfile ? "已保存" : "尚未保存")
                        } }
                    }
                }.frame(width: 310)
            }
            if !model.actionMessage.isEmpty { Label(model.actionMessage, systemImage: model.actionFailed ? "xmark.circle" : "checkmark.circle").font(.subheadline).foregroundStyle(model.actionFailed ? Color.red : archiveGreen) }
        }.padding(34) }
    }
}

struct FormRow<Content: View>: View {
    let number: String; let title: String; let content: Content
    init(number: String, title: String, @ViewBuilder content: () -> Content) { self.number = number; self.title = title; self.content = content() }
    var body: some View { HStack(spacing: 13) {
        Text(number).font(.headline).foregroundStyle(.white).frame(width: 28, height: 28).background(Circle().fill(archiveBlue))
        Text(title).font(.headline).frame(width: 110, alignment: .leading); content
    } }
}

struct SummaryRow: View {
    let title: String; let value: String
    init(_ title: String, _ value: String) { self.title = title; self.value = value }
    var body: some View { HStack { Text(title).foregroundStyle(archiveMuted); Spacer(); Text(value).fontWeight(.medium) } }
}

private func runtimeStateLabel(_ value: String) -> String {
    switch value {
    case "COMPLETE", "DONE": return "已完成"
    case "INGESTING", "RUNNING": return "正在处理"
    case "INGESTED": return "上游完成，继续下游"
    case "TIMELAPSE_COVERED": return "由代表图覆盖，等待闭环"
    case "SKIPPED": return "按规则无需处理"
    case "FAILED", "FAILED_FINAL": return "处理失败"
    case "WAITING_DEPENDENCY": return "等待上游结果"
    default: return "等待处理"
    }
}
struct RuntimeSummaryRow: View {
    let item: RuntimeStageSummary
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack { Text(item.name).font(.subheadline.bold()); Spacer(); Text("完成 \(item.done)").font(.caption) }
            Text("运行 \(item.running) · 等待 \(item.pending) · 可领取 \(item.runnable) · 失败 \(item.failed)")
                .font(.caption2).foregroundStyle(item.failed > 0 ? Color.red : archiveMuted)
            if !item.blocker.isEmpty && item.pending > 0 { Text("等待原因：\(item.blocker)").font(.caption2).foregroundStyle(archiveOrange) }
            if item.stage == "qwen" && item.failed > 0 {
                Text("\(item.failed) 张画面描述未通过输出校验；已成功的结果保留，其他泳道继续处理。失败不等于整个阶段停机。")
                    .font(.caption2).foregroundStyle(Color.red)
            }
        }.padding(.vertical, 4)
    }
}
struct RuntimeLaneCard: View {
    let lane: RuntimeLane
    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 10) {
                Label(lane.name, systemImage: lane.kind == "image" ? "photo" : lane.kind == "video" ? "video" : "waveform").font(.title3.bold())
                Text("\(lane.kind == "audio" ? "音频处理完成" : "视觉处理完成") \(lane.sourceDone) / \(lane.sourceTotal)").font(.headline)
                if lane.sourceTotal == 0 {
                    Text("本任务没有这类素材，无需处理").foregroundStyle(archiveGreen)
                } else {
                    ProgressView(value: Double(lane.sourceDone), total: Double(lane.sourceTotal)).tint(archiveBlue)
                }
                Text("本泳道待处理 \(lane.waiting) · 失败 \(lane.failed)").font(.caption).foregroundStyle(archiveMuted)
                if lane.kind == "image" {
                    Text("原始图片 \(lane.sourceTotal) · 需处理 \(lane.eligible)")
                    Text("延时摄影覆盖 \(lane.covered) · 预处理完成 \(lane.producerDone)")
                } else if lane.kind == "video" {
                    Text("正常视觉视频 \(lane.eligible) · RAW 视觉跳过 \(lane.skipped)")
                    Text("抽帧完成 \(lane.producerDone) · 已生成画面 \(lane.frames)")
                } else {
                    Text("音频来源 \(lane.sourceTotal) · 抽取队列完成 \(lane.producerDone)")
                    Text("无音轨按规则跳过；RAW 音轨继续转写")
                }
                if lane.kind != "audio" { Text("证据齐全并已筛选 \(lane.ready) 个来源").foregroundStyle(archiveGreen) }
                Divider()
                Text(lane.currentItem).font(.caption).foregroundStyle(archiveBlue).lineLimit(3).textSelection(.enabled)
                if !lane.blocker.isEmpty { Text("阻塞原因：\(lane.blocker)").foregroundStyle(archiveOrange) }
                ForEach(lane.stages) { item in RuntimeSummaryRow(item: item) }
            }.font(.caption).frame(maxWidth: .infinity, alignment: .topLeading)
        }.frame(maxWidth: .infinity, alignment: .topLeading)
    }
}
struct RuntimeDashboardView: View {
    @EnvironmentObject var model: ArchiveModel
    let dashboard: RuntimeDashboard
    let stages: [PipelineStage]
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Panel {
                VStack(alignment: .leading, spacing: 10) {
                    Text("项目总览 · 扫描与清单").font(.headline)
                    Text("全部流程完成 \(dashboard.sourceDone) / \(dashboard.sourceTotal) · 需视觉与音频都完成；下面各泳道独立计数")
                    HStack(spacing: 25) {
                        SummaryRow("累计时间（含停止）", formatSeconds(dashboard.timer.totalElapsed))
                        SummaryRow("实际处理", dashboard.timer.activeProcessing.map(formatSeconds) ?? "历史记录不足")
                        SummaryRow("暂停累计", dashboard.timer.pausedTime.map(formatSeconds) ?? "历史记录不足")
                        SummaryRow("当前状态", dashboard.timer.paused ? "已暂停，可继续" : "见上方任务状态")
                    }
                    Text("开始：\(dashboard.timer.taskStart ?? "未记录") · 结束：\(dashboard.timer.taskEnd ?? "尚未结束")").font(.caption).textSelection(.enabled)
                    if let benchmark = dashboard.timer.benchmarkElapsed { Text("正式基准用时：\(formatSeconds(benchmark))（从 T=0 到任务终态）").foregroundStyle(archiveBlue) }
                    Text(dashboard.timer.basis).font(.caption2).foregroundStyle(archiveMuted)
                    if !dashboard.mainBlocker.isEmpty { Text("主要等待原因：\(dashboard.mainBlocker)").foregroundStyle(archiveOrange) }
                    if let control = dashboard.controlSummary { Text(control).font(.caption).foregroundStyle(archiveMuted) }
                    if let highValue = dashboard.highValueSummary { Text(highValue).font(.caption).foregroundStyle(archiveMuted) }
                }
            }
            HStack(alignment: .top, spacing: 14) { ForEach(dashboard.lanes) { lane in RuntimeLaneCard(lane: lane) } }
            HStack(alignment: .top, spacing: 14) {
                Panel {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("图片 + 视频 · 共同视觉下游").font(.headline)
                        Text("一个来源证据齐全即可向下推进，不等待整项目完成。向量任务完成数不等于有效覆盖验收。")
                            .font(.caption).foregroundStyle(archiveMuted)
                        Text(dashboard.visualCoverageStatus ?? "有效覆盖：等待正式验收结果").font(.caption).foregroundStyle(archiveBlue)
                        ForEach(stages.filter { ["visual_schema_v3", "person_reid_optional_v1", "candidate_schema", "candidates_generic_v2", "candidate_snapshot"].contains($0.key) }) { stage in
                            HStack { Text(stage.name); Spacer(); Text(stage.total > 0 ? "\(stage.done)/\(stage.total) · \(stageStatusLabel(stage.status))" : "无逐项队列 · \(stageStatusLabel(stage.status))").font(.caption) }
                            Text(stage.description).font(.caption2).foregroundStyle(archiveMuted)
                        }
                        ForEach(dashboard.stageSummaries.filter { ["qwen", "ocr", "final_projection"].contains($0.stage) }) { item in RuntimeSummaryRow(item: item) }
                        Text("整理搜索内容：把画面描述、识别文字与来源关系组织成可搜索记录；不是重新运行模型。")
                            .font(.caption2).foregroundStyle(archiveMuted)
                    }
                }.frame(maxWidth: .infinity)
                Panel {
                    VStack(alignment: .leading, spacing: 9) {
                        Text("音频下游说明").font(.headline)
                        Text("音频抽取 → 人声检测 → 语音转写 → 文本 → 搜索向量")
                        Text("抽取完成不等于全部音频工作完成。画面与音频最终共同汇入搜索。")
                    }.font(.caption).foregroundStyle(archiveMuted)
                }.frame(width: 270)
            }
            Panel {
                VStack(alignment: .leading, spacing: 8) {
                    Text("最终汇合 · 搜索与索引").font(.headline)
                    Text("画面搜索记录 + 语音文本 → 语义关联 → 搜索向量与全文索引 → 来源完成").font(.caption).foregroundStyle(archiveMuted)
                    ForEach(dashboard.stageSummaries.filter { ["embedding", "final_manifest"].contains($0.stage) }) { item in RuntimeSummaryRow(item: item) }
                }
            }
        }
    }
}
struct RunningPage: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 18) {
            PageHeader(title: "运行状态", subtitle: "显示真实数据库进度；每完成一项就会在中心数据库中更新。")
            if !model.loadError.isEmpty { Panel { Label(model.loadError, systemImage: "exclamationmark.triangle.fill").foregroundStyle(Color.red) } }
            if let state = model.snapshot {
                if let databaseError = state.databaseReadError, !databaseError.isEmpty {
                    Panel { Label("数据库暂时不可读取：\(databaseError)；流水线阶段状态仍会继续显示。", systemImage: "exclamationmark.triangle.fill").foregroundStyle(archiveOrange) }
                }
                if state.configurationState == "first_run_clean" && state.activeRuns.isEmpty {
                    Panel { VStack(spacing: 14) {
                        Image(systemName: "tray").font(.system(size: 42)).foregroundStyle(archiveMuted)
                        Text("还没有运行中的任务").font(.title3.bold())
                        Text("新建第一个素材库后，这里会显示总进度、当前阶段、预计剩余时间和资源状态。").foregroundStyle(archiveMuted)
                        Button("新建第一个任务") { model.page = .newTask }.buttonStyle(PrimaryButtonStyle())
                    }.frame(maxWidth: .infinity).padding(.vertical, 55) }
                } else {
                    Panel { VStack(alignment: .leading, spacing: 12) {
                    let launcherStatus = state.pipeline.fullPipelineLauncherStatus
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(progressStatusDisplay(launcherStatus, active: !state.activeRuns.isEmpty)).font(.title3.bold())
                            let v2Stages = state.pipeline.stages.filter { $0.status == "running" }.count
                            Text(state.pipeline.schedulerMode == "event_driven_v2"
                                 ? (v2Stages > 0 ? "流式异步 V2 · \(v2Stages) 个阶段同时进行" : "流式异步 V2 · 当前无活动阶段")
                                 : (state.activeRuns.isEmpty ? "当前没有正在运行的任务" : "正在处理 \(state.activeRuns.count) 个阶段"))
                                .font(.subheadline).foregroundStyle(archiveMuted)
                        }
                        Spacer(); Text(String(format: "%.1f%%", state.pipeline.overallPercent)).font(.title2.bold()).foregroundStyle(archiveBlue)
                    }
                    if let lifecycle = state.pipeline.lifecycle {
                        Text(state.pipeline.searchReady ? "素材处理已完成" : lifecycle.currentPass == "PRIMARY" ? "正在处理素材" :
                             lifecycle.currentPass == "SWEEP_1" ? "正在重新处理失败项（1/2）" : "正在重新处理失败项（2/2）")
                            .font(.subheadline)
                        if lifecycle.failedFinalCount > 0 {
                            Text("\(lifecycle.failedFinalCount) 项需要人工检查").foregroundStyle(archiveOrange)
                        }
                    }
                    ProgressView(value: state.pipeline.overallPercent, total: 100).tint(archiveBlue)
                    if state.pipeline.schedulerMode == "event_driven_v2" {
                        let total = state.pipeline.currentJobsTotal ?? 0
                        let done = state.pipeline.currentJobsDone ?? 0
                        let running = state.pipeline.currentJobsRunning ?? 0
                        let pending = state.pipeline.currentJobsPending ?? 0
                        Text("调度模式：流式异步 V2").font(.subheadline).foregroundStyle(archiveBlue)
                        let retryable = state.pipeline.currentJobsRetryable ?? 0
                        let failedFinal = state.pipeline.currentJobsFailedFinal ?? 0
                        Text("当前任务：已完成 \(done) / \(total)（当前已生成任务） · 运行中 \(running) · 待处理 \(pending) · 等待重试 \(retryable) · 最终失败 \(failedFinal)")
                            .font(.subheadline).foregroundStyle(archiveMuted)
                        Text(state.pipeline.overallEtaBasis ?? "下游任务会随处理动态生成；当前百分比不是最终项目完成度")
                            .font(.caption).foregroundStyle(archiveMuted)
                    }
                    Text(state.pipeline.searchReady ? "图片与视频搜索已经可用" : "搜索将在所需阶段完成后开放").font(.subheadline).foregroundStyle(archiveMuted)
                    if !state.searchRuntime.ready, let error = state.searchRuntime.databasePreflight?.databaseError, !error.isEmpty {
                        Text("搜索数据库不可用：\(error)").font(.caption).foregroundStyle(archiveOrange)
                    }
                    Text(etaDisplay(state.pipeline.overallEtaSeconds, status: launcherStatus)).font(.headline).foregroundStyle(archiveBlue)
                    if launcherStatus != "SUCCESS" && launcherStatus != "COMPLETED" {
                        Text(state.pipeline.overallEtaBasis ?? "按当前剩余工作量动态估算").font(.caption).foregroundStyle(archiveMuted)
                    }
                    HStack {
                        Spacer()
                        if state.activeRuns.isEmpty && (state.pipeline.currentJobsFailedFinal ?? 0) > 0 {
                            Button("重试一项失败的画面描述") { model.retryOneFailedDescription() }
                                .disabled(model.actionInProgress)
                        }
                        if !state.activeRuns.isEmpty {
                            Button("停止当前任务") { model.stopTask() }.foregroundStyle(Color.red)
                        } else if state.pipeline.fullPipelineLauncherStatus == "MAINTENANCE_SEARCH_INCOMPLETE" {
                            Button("继续补齐搜索向量") { model.resumeTask() }.buttonStyle(PrimaryButtonStyle())
                        } else if ["FAILED", "CANCELLED", "RECOVERABLE", "FAILED_OUTPUT_ACCEPTANCE"].contains(state.pipeline.fullPipelineLauncherStatus ?? "") {
                            Button("从断点继续") { model.resumeTask() }.buttonStyle(PrimaryButtonStyle())
                        } else if state.pipeline.searchReady {
                            Button("进入搜索素材") { model.page = .search }.buttonStyle(PrimaryButtonStyle())
                        }
                    }
                    } }
                    if state.resources.processAlive {
                        Panel { VStack(alignment: .leading, spacing: 8) {
                            Label("内存与运行资源", systemImage: "gauge.with.dots.needle.67percent").font(.headline)
                            Text("整台 Mac 已用（估算）：\(state.resources.systemMemoryUsedBytes.map(formatMemoryGB) ?? "暂不可用") / \(state.resources.systemMemoryTotalBytes.map(formatMemoryGB) ?? "暂不可用")").font(.headline)
                            Text("其中本任务进程内存约：\(formatMemoryGB(state.resources.memoryBytes))").font(.headline)
                            HStack(spacing: 24) {
                                SummaryRow("PID", String(state.resources.activePid ?? 0))
                                SummaryRow("进程树节点", String(state.resources.processCount ?? 1))
                                SummaryRow("CPU 总和（100%=1核）", String(format: "%.1f%%", state.resources.cpuPercent))
                                SummaryRow("系统 Swap", state.resources.swapUsedBytes.map(formatBytes) ?? "系统未公开")
                            }
                            Text("系统已用包含其他软件；本任务为处理进程及模型子进程的 RSS 近似合计，共享内存可能重复计数，不等于独占内存。Swap 是整台 Mac 的系统值。")
                                .font(.caption).foregroundStyle(archiveMuted)
                            ForEach(state.resources.workers ?? []) { worker in
                                Text("\(worker.purpose) · PID \(worker.pid) · CPU \(String(format: "%.1f%%", worker.cpuPercent)) · 进程内存 \(formatBytes(worker.memoryBytes))")
                                    .font(.caption).textSelection(.enabled)
                            }
                            Text("活动监视器可能仍显示 Python；用 PID 可对照上面的真实用途。进程内存不是统一内存的精确总占用。")
                                .font(.caption2).foregroundStyle(archiveMuted)
                        } }
                    }
                if let failedStage = state.pipeline.failedStageName, !failedStage.isEmpty {
                    Panel { VStack(alignment: .leading, spacing: 9) {
                        Label("任务在“\(failedStage)”失败", systemImage: "exclamationmark.triangle.fill")
                            .font(.headline).foregroundStyle(Color.red)
                        Text(state.pipeline.errorSummary ?? "未记录错误摘要")
                            .textSelection(.enabled)
                        if let logPath = state.pipeline.errorLogPath, !logPath.isEmpty {
                            Text("完整日志：\(logPath)")
                                .font(.caption).foregroundStyle(archiveMuted)
                                .textSelection(.enabled)
                        }
                        Button("复制错误信息") {
                            let diagnostic = [
                                "失败阶段：\(failedStage)",
                                "错误摘要：\(state.pipeline.errorSummary ?? "--")",
                                "完整日志：\(state.pipeline.errorLogPath ?? "--")",
                                state.pipeline.errorDetails ?? "",
                            ].joined(separator: "\n")
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(diagnostic, forType: .string)
                        }
                    } }
                }
                if !state.activeRuns.isEmpty {
                    ForEach(state.activeRuns) { run in Panel { HStack {
                        Image(systemName: "circle.dotted").font(.title2).foregroundStyle(archiveBlue)
                        VStack(alignment: .leading) { Text(run.stage ?? "处理中").font(.headline); Text("已用 \(formatSeconds(run.elapsedSeconds)) · \(etaDisplay(run.etaSeconds, status: state.pipeline.fullPipelineLauncherStatus))").font(.caption).foregroundStyle(archiveMuted) }
                        Spacer(); VStack(alignment: .trailing) { Text(String(format: "%.1f%%", run.percent ?? 0)).font(.headline); Text("\(run.completed ?? 0) / \(run.total ?? 0)").font(.caption).foregroundStyle(archiveMuted) }
                    } } }
                }
                HStack { MetricCard(title: "图片", value: (state.overview.source["image"]?.count ?? 0).formatted(), icon: "photo"); MetricCard(title: "视频", value: (state.overview.source["video"]?.count ?? 0).formatted(), icon: "video"); MetricCard(title: "可搜索画面", value: state.overview.visualUnitTotalCount.formatted(), icon: "square.grid.3x3"); MetricCard(title: "失败记录", value: (state.pipeline.failedRecordCount ?? 0).formatted(), tint: (state.pipeline.failedRecordCount ?? 0) == 0 ? archiveGreen : .red, icon: "exclamationmark.triangle") }
                DisclosureGroup("查看失败与重试记录") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("等待重试 \(state.pipeline.lifecycle?.retryableCount ?? 0) · 最终失败来源 \(state.pipeline.lifecycle?.failedFinalCount ?? 0)")
                        if let errors = state.pipeline.lifecycle?.errorSummary, !errors.isEmpty {
                            ForEach(errors.keys.sorted(), id: \.self) { sourceId in
                                Text(state.pipeline.runtimeDashboard?.sources.first(where: { $0.sourceId == sourceId })?.filename ?? sourceId).font(.headline)
                                Text(String((errors[sourceId] ?? "").prefix(1200))).textSelection(.enabled)
                            }
                        } else {
                            Text("当前没有未解决的错误；已经恢复的尝试保留在任务日志中。").foregroundStyle(archiveMuted)
                        }
                    }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
                }.padding()
                    if let dashboard = state.pipeline.runtimeDashboard {
                        RuntimeDashboardView(dashboard: dashboard, stages: state.pipeline.stages)
                    }
                    Panel { VStack(alignment: .leading, spacing: 5) {
                    Text("阶段进度（共 \(state.pipeline.stages.count) 个阶段）").font(.headline).padding(.bottom, 8)
                    ForEach(Array(state.pipeline.stages.enumerated()), id: \.element.id) { index, stage in
                        HStack(spacing: 14) {
                            Image(systemName: stageStatusIcon(stage.status)).font(.title3).foregroundStyle(stageStatusColor(stage.status))
                            VStack(alignment: .leading, spacing: 3) {
                                Text("\(index + 1). \(stage.name)").font(.headline)
                                Text(stage.errorSummary?.isEmpty == false ? stage.errorSummary! : stage.description)
                                    .font(.caption)
                                    .foregroundStyle(stage.status == "failed" ? Color.red : archiveMuted)
                                    .textSelection(.enabled)
                                if ["running", "stopping", "retryable", "drained_open"].contains(stage.status) {
                                    if let item = stage.currentItem, !item.isEmpty {
                                        Text("当前：\(item)").font(.caption2).foregroundStyle(archiveBlue).lineLimit(2)
                                    }
                                    HStack(spacing: 12) {
                                        Text("成功 \(stage.successCount ?? 0)")
                                        Text("跳过 \(stage.skippedCount ?? 0)")
                                        Text("失败 \(stage.failedCount ?? 0)")
                                        if let workers = stage.configuredWorkers { Text("并发上限 \(workers) 路") }
                                        if let workers = stage.actualWorkers { Text("当前工作 \(workers) 路") }
                                        if let ffmpeg = stage.ffmpegProcesses { Text("FFmpeg \(ffmpeg) 个") }
                                        if let models = stage.modelWorkers { Text("模型并发 \(models) 路") }
                                        if let active = stage.activeWorkers, let idle = stage.idleWorkers {
                                            Text("活动 \(active) · 空闲 \(idle)")
                                        }
                                        if let pending = stage.queuePending, let running = stage.queueRunning {
                                            Text("队列待处理 \(pending) · 运行中 \(running)")
                                        }
                                        if let restarts = stage.restartCount, restarts > 0 {
                                            Text("已自动重启 \(restarts) 次").foregroundStyle(Color.orange)
                                        }
                                    }.font(.caption2).foregroundStyle(archiveMuted)
                                    if let bytes = stage.bytesProcessed, bytes > 0 {
                                        Text("本阶段已生成 \(stage.outputFiles ?? 0) 个文件 · \(formatBytes(bytes))")
                                            .font(.caption2).foregroundStyle(archiveMuted)
                                    }
                                    if stage.status == "success" {
                                        if let bytes = stage.stageOutputBytes, bytes > 0 {
                                            Text("阶段输出占用 \(formatBytes(bytes)) · 中心数据库增长 \(formatBytes(stage.databaseDeltaBytes ?? 0))")
                                                .font(.caption2).foregroundStyle(archiveMuted)
                                        }
                                        if stage.reportPaths?["summary"] != nil {
                                            Button("打开阶段完成报告") { model.openStageReport(stage) }
                                                .font(.caption2)
                                        }
                                    }
                                    Text(stage.etaSeconds.map { "预计剩余 \(formatSeconds($0))" } ?? (stage.etaBasis ?? "正在估算"))
                                        .font(.caption2).foregroundStyle(archiveMuted)
                                }
                            }
                            Spacer(); VStack(alignment: .trailing) {
                                Text(stageStatusLabel(stage.status))
                                    .foregroundStyle(stageStatusColor(stage.status))
                                if stage.total > 0 {
                                    Text("\(stage.done.formatted()) / \(stage.total.formatted())").font(.caption2).foregroundStyle(archiveMuted)
                                } else if stage.status == "failed" {
                                    Text("查看上方错误详情").font(.caption2).foregroundStyle(Color.red)
                                } else {
                                    Text("无逐项队列").font(.caption2).foregroundStyle(archiveMuted)
                                }
                            }
                        }.padding(.vertical, 9)
                        if index < state.pipeline.stages.count - 1 { Divider() }
                    }
                    } }
                }
            } else { ProgressView("读取中心数据库状态…") }
        }.padding(34) }
    }
}

struct HistoryPage: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View { ScrollView { VStack(alignment: .leading, spacing: 16) {
        PageHeader(title: "任务历史", subtitle: "只展示中心数据库中真实存在的处理记录。")
        if let libraries = model.snapshot?.existingLibraries, !libraries.isEmpty {
            ForEach(libraries) { library in Panel { HStack(spacing: 14) {
                Image(systemName: library.status == "success" ? "checkmark.circle.fill" : "clock.badge.exclamationmark").font(.title2).foregroundStyle(library.status == "success" ? archiveGreen : archiveOrange)
                VStack(alignment: .leading, spacing: 4) {
                    Text(library.taskName).font(.headline)
                    Text(library.sourceRoot).font(.caption).foregroundStyle(archiveMuted)
                    Text("图片 \(library.imageCount) · 视频 \(library.videoCount) · 总用时 \(library.elapsedHuman ?? "--") · \(library.createdAt)").font(.caption2).foregroundStyle(archiveMuted)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 6) {
                    Text(library.isActive ? "当前搜索库" : library.status).fontWeight(.semibold)
                    Button("查看阶段明细与索引占用") { model.loadHistoryDetail(library) }
                    Button("只读存储审计") { model.loadStorageAudit(library) }
                    Button("生成安全清理计划") { model.loadStorageCleanupPlan(library) }
                    if library.isActive { Button("搜索素材") {model.page = .search}.buttonStyle(.borderedProminent) }
                    if !library.isActive {
                        Button("切换为当前搜索库") { model.activateLibrary(library) }
                            .buttonStyle(.borderedProminent)
                    }
                }
            } } }
            Panel { VStack(alignment: .leading, spacing: 10) {
                Text("历史任务对比").font(.headline)
                Text("分别比较正式产物、数据库、备份、日志和临时文件；不会删除任何内容。")
                    .font(.caption).foregroundStyle(archiveMuted)
                HStack {
                    Picker("左侧任务", selection: $model.comparisonLeftTaskPath) {
                        Text("请选择").tag("")
                        ForEach(libraries) { Text($0.taskName).tag($0.taskPath) }
                    }
                    Picker("右侧任务", selection: $model.comparisonRightTaskPath) {
                        Text("请选择").tag("")
                        ForEach(libraries) { Text($0.taskName).tag($0.taskPath) }
                    }
                    Button("开始只读比较") { model.compareSelectedTasks() }
                }
                if let comparison = model.taskComparison {
                    Text(comparison.interpretation).font(.caption).foregroundStyle(archiveMuted)
                    ForEach(comparison.categoryDifference.keys.sorted(), id: \.self) { key in
                        if let row = comparison.categoryDifference[key] {
                            Text("\(key)：文件 \(row.fileCountDeltaRightMinusLeft >= 0 ? "+" : "")\(row.fileCountDeltaRightMinusLeft)，容量 \(formatBytes(row.bytesDeltaRightMinusLeft))")
                                .font(.caption).textSelection(.enabled)
                        }
                    }
                }
            } }
            if let audit = model.storageAudit {
                Panel { VStack(alignment: .leading, spacing: 8) {
                    Text("\(audit.taskName) · 只读存储审计").font(.headline)
                    Text("共 \(audit.totalFileCount.formatted()) 个文件，\(formatBytes(audit.totalBytes)) · \(audit.policy)")
                        .font(.caption).foregroundStyle(archiveMuted)
                    ForEach(audit.categories.keys.sorted(), id: \.self) { key in
                        if let row = audit.categories[key] {
                            HStack {
                                Text(key); Spacer()
                                Text("\(row.fileCount.formatted()) 项 · \(formatBytes(row.bytes))")
                                if row.safeToRemoveCount > 0 { Text("候选 \(row.safeToRemoveCount)").foregroundStyle(archiveOrange) }
                                if row.affectsResumeCount > 0 { Text("影响恢复").foregroundStyle(Color.red) }
                            }.font(.caption)
                        }
                    }
                } }
            }
            if let plan = model.storageCleanupPlan,
               let library = libraries.first(where: { $0.taskPath == plan.taskPath }) {
                Panel { VStack(alignment: .leading, spacing: 8) {
                    Label("待确认的安全清理计划", systemImage: "trash.slash").font(.headline)
                    Text("候选 \(plan.candidateCount) 项 · \(formatBytes(plan.candidateBytes))；已排除 \(plan.excludedResumeAffectingCount) 项可能影响断点恢复的缓存。")
                        .font(.caption).foregroundStyle(archiveMuted)
                    Text(plan.policy).font(.caption).foregroundStyle(archiveMuted)
                    ForEach(plan.items.prefix(20)) { item in
                        Text("\(item.relativePath) · \(formatBytes(item.bytes)) · \(item.category) · \(item.reason)")
                            .font(.caption2).textSelection(.enabled)
                    }
                    if plan.items.count > 20 {
                        Text("另有 \(plan.items.count - 20) 项；执行时仍逐项重新核对路径、大小和修改时间。")
                            .font(.caption2).foregroundStyle(archiveMuted)
                    }
                    Divider()
                    Text("此操作会永久删除上面明确列出的候选。请输入完整确认短语后按钮才会启用：")
                        .font(.caption).foregroundStyle(Color.red)
                    Text(plan.confirmationPhrase).font(.caption.monospaced()).textSelection(.enabled)
                    TextField("输入确认短语", text: $model.storageCleanupConfirmation)
                    Button("执行已核对的永久清理") { model.applyStorageCleanup(library) }
                        .disabled(model.storageCleanupConfirmation != plan.confirmationPhrase)
                        .foregroundStyle(Color.red)
                } }
            }
            if !model.storageCleanupResult.isEmpty {
                Text(model.storageCleanupResult).foregroundStyle(archiveGreen)
            }
            if !model.storageAuditError.isEmpty { Text(model.storageAuditError).foregroundStyle(archiveOrange) }
            if model.historyLoading { ProgressView("读取所选历史任务…") }
            if !model.historyError.isEmpty { Text(model.historyError).foregroundStyle(.red) }
            if let detail = model.historyDetail {
                Panel { VStack(alignment: .leading, spacing: 6) {
                    Text(detail.taskName).font(.title3.bold())
                    Text("任务状态：\(detail.taskStatus) · 总进度 \(String(format: "%.1f%%", detail.pipeline.overallPercent))").foregroundStyle(archiveMuted)
                    Text("开始：\(detail.startedAt ?? "--") · 结束：\(detail.finishedAt ?? "--") · 总用时：\(detail.elapsedHuman ?? formatSeconds(detail.elapsedSeconds))")
                        .font(.caption).foregroundStyle(archiveMuted)
                    if let storage = detail.indexStorage {
                        Text("索引占用：\(formatBytes(storage.totalBytes)) · \(storage.totalFileCount.formatted()) 个文件（只统计索引任务目录，不读取原始素材）")
                            .font(.caption).foregroundStyle(archiveBlue)
                    }
                    if detail.taskStatus == "failed" {
                        Divider()
                        Label(
                            "失败阶段：\(detail.pipeline.failedStageName ?? "未知阶段")",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .font(.headline).foregroundStyle(Color.red)
                        Text(detail.pipeline.errorSummary ?? detail.error ?? "未记录错误摘要")
                            .textSelection(.enabled)
                        if let logPath = detail.pipeline.errorLogPath, !logPath.isEmpty {
                            Text("完整日志：\(logPath)")
                                .font(.caption).foregroundStyle(archiveMuted)
                                .textSelection(.enabled)
                        }
                        Button("复制错误信息") {
                            let diagnostic = [
                                "失败阶段：\(detail.pipeline.failedStageName ?? "未知阶段")",
                                "错误摘要：\(detail.pipeline.errorSummary ?? detail.error ?? "--")",
                                "完整日志：\(detail.pipeline.errorLogPath ?? "--")",
                                detail.pipeline.errorDetails ?? "",
                            ].joined(separator: "\n")
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(diagnostic, forType: .string)
                        }
                    }
                    ForEach(Array(detail.pipeline.stages.enumerated()), id: \.element.id) { index, stage in
                        Divider()
                        HStack(alignment: .top) {
                            Image(systemName: stageStatusIcon(stage.status)).foregroundStyle(stageStatusColor(stage.status))
                            VStack(alignment: .leading, spacing: 3) {
                                Text("\(index + 1). \(stage.name)").font(.headline)
                                Text(stage.description).font(.caption).foregroundStyle(archiveMuted)
                            }
                            Spacer()
                            if stage.total > 0 { Text("\(stage.done) / \(stage.total)").font(.caption).foregroundStyle(archiveMuted) }
                        }.padding(.vertical, 5)
                    }
                } }
            }
        } else { Panel { Text("还没有可显示的任务记录。") } }
    }.padding(34) } }
}

struct LocalFramePreview: View {
    let path: String
    @Environment(\.dismiss) private var dismiss
    @State private var image: NSImage?
    @State private var loaded = false
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("本地画面预览").font(.headline)
                Spacer()
                Button { dismiss() } label: { Label("关闭", systemImage: "xmark") }
                    .keyboardShortcut(.cancelAction)
            }.padding(12)
            ZStack {
                Color.black
                if let image { Image(nsImage: image).resizable().scaledToFit() }
                else if loaded { Text("本地预览文件暂不可用").foregroundStyle(.white) }
                else { ProgressView() }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(width: max(320, min(1000, (NSApp.mainWindow?.contentLayoutRect.width ?? 800) - 40)),
               height: max(240, min(700, (NSApp.mainWindow?.contentLayoutRect.height ?? 600) - 70)))
        .onAppear { image = NSImage(contentsOfFile: path); loaded = true }
    }
}

struct SearchResultCard: View {
    @Environment(\.responsiveWidth) private var width
    @EnvironmentObject var model: ArchiveModel; let result: SearchResult
    @State private var selectedMoment: SearchMatchCluster?
    @State private var localFramePreviewPresented = false
    private var activeTime: Int? { selectedMoment?.representativeTimeMs ?? result.timePositionMs }
    private var activePreview: String? { selectedMoment == nil ? (result.representativeThumbnail ?? result.previewPath) : selectedMoment?.representativeFrame?.previewPath }
    @State private var annotationTags: String
    @State private var annotationNote: String
    @State private var annotationFavorite: Bool
    @State private var annotationRating: Int
    @State private var annotationIgnored: Bool
    @State private var annotationSaving = false
    @State private var annotationSaveState = ""
    @State private var annotationSaveMessage = ""
    @State private var manualPersonTargetId = ""
    @State private var manualPersonName = ""
    @State private var manualPersonTags = ""
    @State private var manualPersonStatus = ""
    @State private var manualPersonFailed = false
    init(result: SearchResult) {
        self.result = result
        _annotationTags = State(initialValue: (result.userAnnotation?.tags ?? []).joined(separator: "，"))
        _annotationNote = State(initialValue: result.userAnnotation?.note ?? "")
        _annotationFavorite = State(initialValue: result.userAnnotation?.favorite ?? false)
        _annotationRating = State(initialValue: result.userAnnotation?.rating ?? 0)
        _annotationIgnored = State(initialValue: result.userAnnotation?.ignored ?? false)
    }
    private var visibleReasons: [String] {
        (result.relevanceReasons ?? []).filter {
            $0 != "exact_object_label" || !(result.matchedObjectLabels ?? []).isEmpty
        }
    }
    private func audioTimecode(_ milliseconds: Int?) -> String {
        guard let milliseconds else { return "--" }
        let value = max(0, milliseconds)
        let hours = value / 3_600_000
        let minutes = (value % 3_600_000) / 60_000
        let seconds = (value % 60_000) / 1_000
        let millis = value % 1_000
        return hours > 0
            ? String(format: "%02d:%02d:%02d.%03d", hours, minutes, seconds, millis)
            : String(format: "%02d:%02d.%03d", minutes, seconds, millis)
    }
    @State private var momentLimit = 12
    @State private var momentsExpanded = false
    private var sourceTitle: String {
        if let displayName = result.displayName, !displayName.isEmpty { return displayName }
        return URL(fileURLWithPath: result.originalPath ?? result.sourceRelativePath ?? "素材").lastPathComponent
    }
    private func reasonLabel(_ reason: SearchMatchReason) -> String {
        let labels = ["OBJECT": "画面对象", "SEMANTIC_VISUAL": "视觉语义", "QWEN": "场景描述", "OCR": "文字命中", "TRANSCRIPT": "对白原文", "TEXT_SEMANTIC":"文本语义", "VISUAL_SEMANTIC":"OpenCLIP视觉语义", "VISUAL_DESCRIPTION":"画面直接描述", "EXPANDED_KEYWORD":"扩展关键词 / 同义线索", "PERSON": "人物", "METADATA": "元数据"]
        let kind = labels[reason.type?.uppercased() ?? ""] ?? reason.type ?? "匹配证据"
        return [kind, reason.label ?? reason.text].compactMap { $0 }.joined(separator: "：")
    }
    private func clusterRange(_ cluster: SearchMatchCluster) -> String {
        cluster.startTimeMs == cluster.endTimeMs ? "匹配时点 \(audioTimecode(cluster.startTimeMs))" : "文字片段 \(audioTimecode(cluster.startTimeMs)) – \(audioTimecode(cluster.endTimeMs))"
    }
    private var evidenceLabel: String {
        let types = Set((selectedMoment?.matchReasons ?? result.matchReasons ?? []).compactMap { $0.type })
        if activeCategory == "dialogue" {
            return types.contains("TRANSCRIPT") ? (types.contains("TEXT_SEMANTIC") ? "对白原文 · 语义支持" : "对白原文匹配") : "对白语义匹配"
        }
        return ["expanded_visual_clue":"扩展关键词 / 同义线索", "visual_description":"画面描述匹配", "visual_semantic":"视觉语义匹配", "dialogue":"对白匹配", "ocr":"画面文字匹配", "neighbor_clue":"邻帧线索", "filename":"文件名匹配", "other_clue":"其他线索"][selectedMoment?.evidenceCategory ?? result.evidenceCategory ?? ""] ?? "匹配线索"
    }
    private func conciseEvidence(_ text: String, category: String?) -> String {
        if category == "visual_semantic" { return "当前帧的视觉向量与查询相似；请结合预览核验。" }
        if category == "neighbor_clue" { return "来自相邻画面的线索，不能确认此帧包含所述对象。" }
        if category == "filename" { return "素材文件名符合查询；不代表画面内容。" }
        if category == "expanded_visual_clue" { return "扩展关键词/同义线索：" + String(text.prefix(240)) }
        if category == "dialogue" { return "对白：" + String(text.prefix(180)) }
        let lines = text.components(separatedBy: "\n")
        let scene = lines.first { $0.hasPrefix("画面描述：") && $0.count > 5 }
        return String((scene ?? text).prefix(180))
    }
    private var activeCategory: String { selectedMoment?.evidenceCategory ?? result.evidenceCategory ?? "" }
    private var locationLabel: String {
        if activeCategory == "dialogue" { return "对白片段 \(audioTimecode(activeTime)) – \(audioTimecode(selectedMoment?.endTimeMs ?? result.resultEndMs))" }
        let label = activeCategory == "filename" ? "素材预览" : (["neighbor_clue", "other_clue", "expanded_visual_clue"].contains(activeCategory) ? "线索预览" : "命中画面")
        return "\(label) \(audioTimecode(activeTime))"
    }
    private var conciseReason: String {
        conciseEvidence(selectedMoment?.evidenceText ?? result.textPreview ?? "", category: selectedMoment?.evidenceCategory ?? result.evidenceCategory)
    }
    var body: some View {
        ResponsiveRow(alignment: .top, spacing: 16) {
            Group {
                // Search results show the original preview; face boxes belong to evidence review only.
                if let path = activePreview, let image = NSImage(contentsOfFile: path) { Image(nsImage: image).resizable().scaledToFill() }
                else { ZStack { Color.gray.opacity(0.15); VStack(spacing: 8) { Image(systemName: "photo").font(.largeTitle); Text("本地预览缺失").font(.caption) }.foregroundStyle(archiveMuted) } }
            }.frame(width: 256, height: 144).clipped().clipShape(RoundedRectangle(cornerRadius: 9))
            VStack(alignment: .leading, spacing: 8) {
                ResponsiveRow {
                    ContentText(sourceTitle).font(.headline).fixedSize(horizontal: false, vertical: true)
                    Spacer()
                    HStack {
                    Image(systemName: result.mediaType == "video" ? "video.fill" : "photo.fill").accessibilityLabel(result.mediaType == "video" ? "视频素材" : "图片素材").foregroundStyle(result.mediaType == "video" ? archiveBlue : archiveGreen).fixedSize()
                    Toggle("选入导出", isOn: Binding(
                        get: { model.isSelectedForExport(result) },
                        set: { model.setSelectedForExport(result, selected: $0) }
                    )).toggleStyle(.checkbox)
                    Text(evidenceLabel).font(.caption).foregroundStyle(archiveBlue)
                    }
                }
                if result.mediaType == "video" {
                    Text(locationLabel)
                        .font(.subheadline).foregroundStyle(archiveMuted)
                }
                if let count = result.otherMatchCount, count > 0 {
                    DisclosureGroup("另外 \(count) 处匹配", isExpanded: $momentsExpanded) {
                        VStack(alignment: .leading, spacing: 7) {
                            ForEach(Array((result.otherMatchClusters ?? result.matchClusters ?? []).prefix(momentLimit))) { cluster in
                                ResponsiveRow(alignment: .top, spacing: 8) {
                                    Button {
                                        selectedMoment=cluster
                                    } label: {
                                        if let path=cluster.representativeFrame?.previewPath, let image=NSImage(contentsOfFile:path) {
                                            Image(nsImage:image).resizable().scaledToFit().frame(width:100,height:60)
                                        } else { Label("预览缺失",systemImage:"photo").frame(width:100,height:60) }
                                    }.buttonStyle(.plain)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(clusterRange(cluster)).font(.subheadline.monospacedDigit())
                                        ContentText(conciseEvidence(cluster.evidenceText ?? "本时点的匹配证据", category: cluster.evidenceCategory))
                                            .font(.caption).foregroundStyle(archiveMuted)
                                        if let reasons = cluster.matchReasons, !reasons.isEmpty {
                                            DisclosureGroup("技术诊断") { ContentText(reasons.map(reasonLabel).joined(separator: "、")).font(.caption).foregroundStyle(archiveMuted) }
                                        }
                                    }
                                }
                            }
                        }.padding(.top, 6)
                    }
                }
                if let summary = result.evidenceSummary { ContentText("匹配依据：" + summary).font(.caption).foregroundStyle(archiveMuted) }
                if momentsExpanded && (result.otherMatchClusters ?? []).count > momentLimit {
                    Button("显示更多命中时点") { momentLimit += 12 }
                }
                ContentText(conciseReason).font(.subheadline).lineLimit(3)
                if activeCategory == "dialogue" { Text("缩略图仅作片段预览，不代表对白提及的物体出现在画面中。").font(.caption).foregroundStyle(archiveMuted) }
                if activeCategory == "expanded_visual_clue" { Text("扩展文本或同义线索命中；请核对原始画面，不能据此断言画面直接包含查询内容。").font(.caption).foregroundStyle(archiveMuted) }
                if let reasons = selectedMoment?.matchReasons ?? result.matchReasons, !reasons.isEmpty {
                    DisclosureGroup("技术诊断：原始证据与排序") {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(reasons) { reason in
                                Label(model.showOriginalContent ? reasonLabel(reason) : simplifiedContent(reasonLabel(reason)), systemImage: "info.circle")
                                    .font(.caption).foregroundStyle(archiveBlue)
                            }
                        }.padding(.top, 4)
                    }
                }
                if let labels = result.matchedObjectLabels, !labels.isEmpty {
                    ContentText("物体标签证据：" + labels.prefix(3).map {
                        "\($0.labelZh ?? $0.label ?? "未知标签")（\(Int(($0.confidence ?? 0) * 100))%）"
                    }.joined(separator: "、"))
                        .font(.caption).foregroundStyle(archiveMuted)
                }
                if let terms = result.matchedTextTerms, !terms.isEmpty {
                    ContentText((result.audioTranscriptMatch == true ? "音频转写命中词：" : "文字证据：") + terms.prefix(4).joined(separator: "、"))
                        .font(.caption).foregroundStyle(archiveMuted)
                }
                let channelScores = [
                    result.openclipCosine.map { "画面原始相似度 \(String(format: "%.3f", $0))" },
                    result.textSemanticScore.map { "描述原始相似度 \(String(format: "%.3f", $0))" },
                ].compactMap { $0 }
                if !channelScores.isEmpty {
                    DisclosureGroup("技术诊断：原始分数") { Text(channelScores.joined(separator: " · ") + "；仅用于排序，不是概率。")
                        .font(.caption2).foregroundStyle(archiveMuted) }
                }
                if let clusters = result.personClusters, !clusters.isEmpty {
                    ForEach(clusters) { cluster in
                        ResponsiveRow {
                            Button("查找 \(cluster.displayName.isEmpty ? "同一人物" : cluster.displayName)（\(cluster.memberCount) 个画面 / \(cluster.distinctSourceCount) 个素材）") {
                                model.searchPersonCluster(cluster.personClusterId)
                            }
                            .buttonStyle(.bordered)
                            .help("只读取本地人物组；人工人物不会改写机器识别结果")
                            if cluster.manualAssignment == true {
                                Button("移出人工人物") {
                                    model.removeResult(result, fromPerson: cluster.personClusterId) { success, message in
                                        manualPersonFailed = !success; manualPersonStatus = message
                                    }
                                }.disabled(true).help("暂不可用：尚无 current-safe 边界").buttonStyle(.borderless)
                            }
                        }
                    }
                }
                if result.resultLevel == "source", let frames = result.sourceFrameCount, frames > 1,
                   let cluster = result.personClusters?.first, let sourceId = result.sourceContentId {
                    Button("展开这个素材中的 \(frames) 个画面") {
                        model.searchPersonCluster(cluster.personClusterId, sourceContentId: sourceId)
                    }.buttonStyle(.bordered)
                }
                ContentText("所属位置：\(result.sourceRelativePath ?? "")").font(.caption2).foregroundStyle(archiveMuted)
                if result.visualUnitId?.isEmpty == false {
                    DisclosureGroup("人工人物归类") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("机器没有识别到正脸也可以人工归类；一张画面可加入多个人物，结果只保存在本机。")
                                .font(.caption).foregroundStyle(archiveMuted)
                            ResponsiveRow {
                                Picker("加入已有人物", selection: $manualPersonTargetId) {
                                    Text("请选择人物").tag("")
                                    ForEach(model.personClusterCatalog) { person in
                                        Text(person.displayName).tag(person.personClusterId)
                                    }
                                }.frame(maxWidth: 340)
                                Button("加入所选人物") {
                                    model.addResult(result, toPerson: manualPersonTargetId) { success, message in
                                        manualPersonFailed = !success; manualPersonStatus = message
                                    }
                                }.disabled(true).help("暂不可用：尚无 current-safe 边界").disabled(manualPersonTargetId.isEmpty)
                            }
                            ResponsiveRow {
                                TextField("新人物名称", text: $manualPersonName)
                                    .textFieldStyle(.roundedBorder).frame(maxWidth: 220)
                                TextField("人物标签（可选）", text: $manualPersonTags)
                                    .textFieldStyle(.roundedBorder).frame(maxWidth: 260)
                                Button("用此画面新建人物") {
                                    let cleanName = manualPersonName.trimmingCharacters(in: .whitespacesAndNewlines)
                                    guard !cleanName.isEmpty else {
                                        manualPersonFailed = true; manualPersonStatus = "请填写新人物名称"; return
                                    }
                                    model.createPerson(from: result, name: cleanName, tags: manualPersonTags) { success, message in
                                        manualPersonFailed = !success; manualPersonStatus = message
                                        if success { manualPersonName = ""; manualPersonTags = "" }
                                    }
                                }.disabled(true).help("暂不可用：尚无 current-safe 边界")
                            }
                            if !manualPersonStatus.isEmpty {
                                Label(
                                    manualPersonStatus,
                                    systemImage: manualPersonFailed ? "exclamationmark.triangle.fill" : "checkmark.circle.fill"
                                ).font(.caption).foregroundStyle(manualPersonFailed ? .red : archiveGreen)
                            }
                        }.padding(.top, 8)
                    }
                }
                DisclosureGroup("本地标签、备注与收藏") {
                    VStack(alignment: .leading, spacing: 8) {
                        TextField("标签，用逗号分隔", text: $annotationTags)
                            .textFieldStyle(.roundedBorder)
                        TextField("备注", text: $annotationNote)
                            .textFieldStyle(.roundedBorder)
                        ResponsiveRow {
                            Toggle("收藏", isOn: $annotationFavorite).toggleStyle(.checkbox)
                            Picker("星级", selection: $annotationRating) {
                                ForEach(0...5, id: \.self) { Text($0 == 0 ? "未评分" : "\($0) 星").tag($0) }
                            }.frame(width: 150)
                            Toggle("忽略", isOn: $annotationIgnored).toggleStyle(.checkbox)
                            Button(annotationSaving ? "保存中…" : "保存") {
                                annotationSaving = true
                                annotationSaveState = ""
                                annotationSaveMessage = ""
                                model.saveResultAnnotation(
                                    result, tags: annotationTags, note: annotationNote,
                                    favorite: annotationFavorite, rating: annotationRating,
                                    ignored: annotationIgnored
                                ) { success, message in
                                    annotationSaving = false
                                    annotationSaveState = success ? "success" : "failure"
                                    annotationSaveMessage = message
                                }
                            }.disabled(annotationSaving)
                            if annotationSaving {
                                ProgressView().controlSize(.small)
                            } else if annotationSaveState == "success" {
                                Label("已保存", systemImage: "checkmark.circle.fill")
                                    .font(.caption.bold()).foregroundStyle(archiveGreen)
                            } else if annotationSaveState == "failure" {
                                Label(annotationSaveMessage, systemImage: "exclamationmark.triangle.fill")
                                    .font(.caption).foregroundStyle(.red)
                            }
                        }
                    }.padding(.top, 8)
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), alignment: .leading)], alignment: .leading, spacing: 10) {
                    Button(result.mediaType == "video" ? "从命中时点播放" : "打开图片") {
                        model.openSearchMoment(result,at:activeTime)
                    }.buttonStyle(PrimaryButtonStyle()).disabled(result.canOpenOriginal != true)
                    if let preview=activePreview {
                        Button("放大本地预览") { localFramePreviewPresented = true }
                            .sheet(isPresented: $localFramePreviewPresented) { LocalFramePreview(path: preview) }
                    }
                    if selectedMoment != nil { Button("返回主命中") { selectedMoment=nil } }
                    Button("在 Finder 中显示") { model.reveal(result) }
                        .disabled(result.canOpenOriginal != true)
                    if result.mediaType == "video", let sourceId = result.sourceContentId {
                        Button("浏览该视频全部画面") { model.browseSourceFrames(sourceId) }
                            .buttonStyle(.bordered)
                        Button("查看视频内容") { model.loadVideoContentTimeline(sourceId: sourceId) }
                            .buttonStyle(.borderedProminent)
                    }
                }
            }.frame(minWidth:0,maxWidth:.infinity,alignment:.leading).environment(\.responsiveWidth, width < 900 ? width - 52 : width - 352)
        }.padding(14).background(Color(nsColor: .controlBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.black.opacity(0.07)))
    }
}

private func videoTimelineTimecode(_ milliseconds: Int) -> String {
    let value = max(0, milliseconds)
    let hours = value / 3_600_000
    let minutes = (value % 3_600_000) / 60_000
    let seconds = (value % 60_000) / 1_000
    return hours > 0
        ? String(format: "%02d:%02d:%02d", hours, minutes, seconds)
        : String(format: "%02d:%02d", minutes, seconds)
}

private func videoTimelineRange(_ start: Int, _ end: Int) -> String {
    "\(videoTimelineTimecode(start)) – \(videoTimelineTimecode(end))"
}

struct VideoContentTimelineSheet: View {
    @EnvironmentObject var model: ArchiveModel
    @Environment(\.dismiss) private var dismiss

    private func evidenceLabel(_ kind: String) -> String {
        switch kind.lowercased() {
        case "whisper": return "对白"
        case "qwen": return "视觉描述"
        case "ocr": return "画面文字"
        case "objects": return "对象"
        case "people": return "人物"
        default: return kind
        }
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    private func evidenceChips(_ timeline: VideoContentTimeline) -> some View {
        HStack(spacing: 6) {
            if timeline.evidenceSummary.whisperDocumentCount > 0 { Label("有对白", systemImage: "waveform").foregroundStyle(.purple) }
            if timeline.evidenceSummary.qwenDocumentCount > 0 { Label("有视觉描述", systemImage: "eye").foregroundStyle(archiveBlue) }
            if timeline.evidenceSummary.yoloeEvidenceCount > 0 { Label("有对象识别", systemImage: "tag").foregroundStyle(archiveGreen) }
            if timeline.evidenceSummary.ocrDocumentCount > 0 { Label("有 OCR", systemImage: "text.viewfinder").foregroundStyle(archiveOrange) }
            if timeline.evidenceSummary.personEvidenceCount > 0 { Label("有人物信息", systemImage: "person").foregroundStyle(.pink) }
        }.font(.caption).labelStyle(.titleAndIcon).fixedSize(horizontal: false, vertical: true)
    }

    private func segmentRow(_ segment: VideoContentSegment, timeline: VideoContentTimeline) -> some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 9) {
                if let visual = segment.visualDescription, !visual.isEmpty {
                    Text("画面描述").font(.caption.bold()).foregroundStyle(archiveMuted)
                    ContentText(visual).textSelection(.enabled)
                }
                if let transcript = segment.transcriptText, !transcript.isEmpty {
                    Text("对白 / 转写").font(.caption.bold()).foregroundStyle(archiveMuted)
                    ContentText(transcript).textSelection(.enabled)
                }
                if let ocr = segment.ocrText, !ocr.isEmpty {
                    Text("画面文字").font(.caption.bold()).foregroundStyle(archiveMuted)
                    ContentText(ocr).textSelection(.enabled)
                }
                if !segment.objects.isEmpty {
                    Text("画面对象：" + segment.objects.map { item in
                        item.confidence.map { "\(item.label)（\(Int($0 * 100))%）" } ?? item.label
                    }.joined(separator: "、"))
                        .font(.subheadline)
                }
                if !segment.people.isEmpty {
                    Text("人物：" + segment.people.map { $0.personId }.joined(separator: "、"))
                        .font(.subheadline)
                }
                DisclosureGroup("分析依据") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(segment.evidenceRefs) { ref in
                            Text(evidenceLabel(ref.stage ?? "证据") + (ref.sourceRevision.map { " · 当前修订 \($0.prefix(12))" } ?? ""))
                                .font(.caption).foregroundStyle(archiveMuted)
                        }
                    }.padding(.top, 4)
                }
            }.padding(.top, 7)
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Button(videoTimelineRange(segment.startTimeMs, segment.endTimeMs)) {
                    model.openTimelineVideo(timeline, at: segment.startTimeMs)
                }.buttonStyle(.borderless).font(.subheadline.monospacedDigit())
                    .disabled(!(timeline.canOpenOriginal == true || timeline.sourceOnline))
                VStack(alignment: .leading, spacing: 4) {
                    ContentText(segment.title).font(.headline)
                    if let description = segment.shortDescription, !description.isEmpty {
                        ContentText(description).font(.subheadline).foregroundStyle(archiveMuted).lineLimit(3)
                    }
                    if let types = segment.provenance.evidenceTypes, !types.isEmpty {
                        Text(types.map(evidenceLabel).joined(separator: " · "))
                            .font(.caption).foregroundStyle(archiveBlue)
                    }
                }
            }
        }
    }

    var body: some View {
        VStack(spacing:0) {
            HStack { Text("视频内容").font(.headline); ContentLanguageControl(); Spacer(); Button("关闭") { dismiss() } }.padding()
            Divider()
            Group {
                if model.videoContentLoading {
                    VStack(spacing: 12) { ProgressView(); Text("正在读取视频内容…").foregroundStyle(archiveMuted) }
                } else if !model.videoContentError.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "exclamationmark.triangle").font(.largeTitle).foregroundStyle(archiveOrange)
                        Text("无法读取视频内容").font(.headline)
                        Text(model.videoContentError).font(.subheadline).foregroundStyle(archiveMuted)
                        Button("重试") { model.loadVideoContentTimeline(sourceId: model.videoContentSourceId, expectedRevision: model.videoContentExpectedRevision) }
                    }.padding()
                } else if let timeline = model.videoContentTimeline {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 18) {
                            VStack(alignment: .leading, spacing: 7) {
                                HStack(alignment: .firstTextBaseline) {
                                    Image(systemName: "video.fill").accessibilityLabel("视频素材").foregroundStyle(archiveBlue)
                                    ContentText(timeline.fileName).font(.title2.bold())
                                    Spacer()
                                    Label(timeline.sourceAvailability == "unchecked" ? "原文件状态未检查；点击打开时读取" : timeline.sourceOnline ? "原始文件可用" : "原始文件当前不可用", systemImage: timeline.sourceOnline ? "checkmark.circle" : "externaldrive.badge.xmark")
                                        .font(.caption).foregroundStyle(timeline.sourceOnline ? archiveGreen : archiveOrange)
                                }
                                if let duration = timeline.videoDurationMs { Text("时长 \(videoTimelineTimecode(duration))").font(.subheadline).foregroundStyle(archiveMuted) }
                                if !timeline.relativePath.isEmpty { ContentText(timeline.relativePath).font(.caption).foregroundStyle(archiveMuted).lineLimit(2) }
                                HStack {
                                    Button("打开视频") { model.openTimelineVideo(timeline, at: nil) }.disabled(!(timeline.canOpenOriginal == true || timeline.sourceOnline))
                                    Button("在 Finder 中显示") { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: timeline.sourcePath)]) }.disabled(!(timeline.canOpenOriginal == true || timeline.sourceOnline))
                                }
                            }
                            Divider()
                            VStack(alignment: .leading, spacing: 8) {
                                Text("内容概览").font(.title3.bold())
                                ContentText(timeline.overview.isEmpty ? "暂无足够内容证据" : timeline.overview)
                                    .foregroundStyle(timeline.overview.isEmpty ? archiveMuted : .primary)
                                evidenceChips(timeline)
                            }
                            if !timeline.segments.isEmpty {
                                VStack(alignment: .leading, spacing: 10) {
                                    Text("内容时间轴").font(.title3.bold())
                                    Text("按时间接近程度归并的内容片段，不代表镜头或场景切分。").font(.caption).foregroundStyle(archiveMuted)
                                    ForEach(timeline.segments) { segment in segmentRow(segment, timeline: timeline) }
                                }
                            }
                            if !timeline.transcriptMinutes.isEmpty {
                                VStack(alignment: .leading, spacing: 10) {
                                    Text(model.showOriginalContent ? "逐分钟原话" : "逐分钟转写（简体显示）").font(.title3.bold())
                                    ForEach(timeline.transcriptMinutes) { minute in
                                        HStack(alignment: .top, spacing: 9) {
                                            Button(videoTimelineRange(minute.startTimeMs, minute.endTimeMs)) { model.openTimelineVideo(timeline, at: minute.startTimeMs) }
                                                .buttonStyle(.borderless).font(.subheadline.monospacedDigit()).disabled(!(timeline.canOpenOriginal == true || timeline.sourceOnline))
                                            ContentText(minute.text).textSelection(.enabled)
                                            Button("复制原文") { copy("\(videoTimelineRange(minute.startTimeMs, minute.endTimeMs)) \(minute.text)") }.buttonStyle(.borderless)
                                        }
                                    }
                                }
                            }
                            if !timeline.keyMoments.isEmpty {
                                VStack(alignment: .leading, spacing: 8) {
                                    Text("关键位置").font(.title3.bold())
                                    ForEach(timeline.keyMoments) { moment in
                                        Button("\(videoTimelineRange(moment.startTimeMs, moment.endTimeMs)) · \(moment.reasonTypes.map(evidenceLabel).joined(separator: " · "))") {
                                            model.openTimelineVideo(timeline, at: moment.startTimeMs)
                                        }.disabled(!(timeline.canOpenOriginal == true || timeline.sourceOnline))
                                    }
                                }
                            }
                            if timeline.segments.isEmpty && timeline.transcriptMinutes.isEmpty {
                                VStack(spacing: 8) {
                                    Image(systemName: "text.bubble").font(.title2).foregroundStyle(archiveMuted)
                                    Text("该视频目前没有可用的分析内容").foregroundStyle(archiveMuted)
                                }.frame(maxWidth: .infinity).padding(.vertical, 28)
                            }
                            if !timeline.qaClues.isEmpty {
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("问答线索").font(.title3.bold())
                                    ForEach(timeline.qaClues, id: \.self) { ContentText($0) }
                                }
                            }
                        }.padding(24)
                    }
                } else {
                    VStack(spacing: 8) {
                        Image(systemName: "video").font(.largeTitle).foregroundStyle(archiveMuted)
                        Text("暂无视频内容").foregroundStyle(archiveMuted)
                    }
                }
            }

        }.frame(minWidth: 420, idealWidth: 760, maxWidth: 900, minHeight: 300, idealHeight: 560, maxHeight: 700)
    }
}

struct SearchPage: View {
    @State private var showValidationLibraries = false
    @State private var showSearchDiagnostics = false
    private var visibleLibraries: [ExistingLibrary] {
        model.libraryCatalog.filter { showValidationLibraries || !$0.isValidationLibrary || $0.isActive }
    }
    @EnvironmentObject var model: ArchiveModel
    @Environment(\.responsiveWidth) private var width
    var body: some View { ScrollView { VStack(alignment: .leading, spacing: 18) {
        VStack(alignment: .leading, spacing: 8) {
            Text("搜索素材").font(.system(size: width < 900 ? 24 : 32, weight: .bold))
            if width >= 900 { Text("默认搜索当前库；也可勾选多个库联合搜索。人物分组始终属于各自的库，不自动跨库合并。").font(.subheadline).foregroundStyle(archiveMuted) }
        }
        DisclosureGroup("选择搜索范围（未勾选时仅当前库）") {
            HStack {
                Button("选择当前列表全部库") {model.selectedSearchLibraries=Set(visibleLibraries.map{$0.taskPath})}
                Button("仅当前库") {model.selectedSearchLibraries=[]}
            }
            Text("选择要搜索的素材库；没有可搜索内容的库会跳过，其他库仍可正常搜索。")
                .font(.caption).foregroundStyle(archiveMuted)
            Toggle("显示历史验收与测试库", isOn: $showValidationLibraries)
                .font(.caption)
                .onChange(of: showValidationLibraries) { shown in
                    if !shown { model.selectedSearchLibraries.subtract(model.libraryCatalog.filter { $0.isValidationLibrary && !$0.isActive }.map { $0.taskPath }) }
                }
            ForEach(visibleLibraries) { library in
                Toggle(simplifiedContent(library.taskName), isOn: Binding(get: {
                    model.selectedSearchLibraries.contains(library.taskPath)
                }, set: { checked in
                    if checked { model.selectedSearchLibraries.insert(library.taskPath) }
                    else { model.selectedSearchLibraries.remove(library.taskPath) }
                }))
                if let state=model.libraryPreparation[library.taskPath] {Text(state).font(.caption).foregroundStyle(archiveMuted)}
            }
        }
        if !model.selectedSearchLibraries.isEmpty {
            Label("已选择 \(model.selectedSearchLibraries.count) 个素材库",systemImage:"externaldrive.fill").font(.subheadline).foregroundStyle(archiveBlue)
        }
        if model.selectedSearchLibraries.isEmpty,let activeLibrary = model.snapshot?.existingLibraries.first(where: { $0.isActive }) {
            Label("当前搜索库：\(activeLibrary.taskName)（图片 \(activeLibrary.imageCount)，视频 \(activeLibrary.videoCount)）", systemImage: "externaldrive.fill")
                .font(.subheadline).foregroundStyle(archiveBlue)
            if let state=model.libraryPreparation[activeLibrary.taskPath],model.selectedSearchLibraries.isEmpty {Text(state).font(.caption).foregroundStyle(archiveMuted)}
        }
        if model.searchNavigationDepth > 0 {
            Panel { ResponsiveRow(spacing: 12) {
                Button("返回上一层") { model.navigateBackInSearch() }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.searching)
                Text("返回后恢复：\(model.searchNavigationTitle)（不会重新搜索）")
                    .font(.subheadline).foregroundStyle(archiveMuted)
                Spacer()
                Text("当前第 \(model.searchNavigationDepth + 1) 层")
                    .font(.caption).foregroundStyle(archiveMuted)
            }}
        }
        Panel { VStack(spacing: 14) {
            HStack { Image(systemName: "magnifyingglass").foregroundStyle(archiveMuted); TextField("例如：夜间户外戴眼镜的人物", text: $model.query).textFieldStyle(.plain).font(.title3).onSubmit { model.search() }; Button(model.searching ? "搜索中…" : "搜索") { model.search() }.buttonStyle(PrimaryButtonStyle()).disabled(model.searching || (model.selectedSearchLibraries.isEmpty && model.snapshot?.searchRuntime.ready != true)) }
            Divider(); LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), alignment: .leading)], alignment: .leading, spacing: 10) {
                Picker("想找什么", selection: Binding(get: { model.searchIntent }, set: { value in guard !model.searching, value != model.searchIntent else { return }; model.searchIntent = value; model.changeSearchIntent() })) { Text("画面内容").tag("visual"); Text("对白与文字").tag("text"); Text("综合搜索").tag("all") }.frame(maxWidth: .infinity).disabled(model.searching)
                Picker("素材类型", selection: $model.mediaType) { ForEach(["全部", "视频", "图片"], id: \.self) { Text($0) } }.frame(maxWidth: .infinity)
                Picker("预览区间", selection: $model.previewWindow) { ForEach(["5 秒", "10 秒"], id: \.self) { Text($0) } }.frame(maxWidth: .infinity)
            }
            DisclosureGroup("高级过滤（由数据库查询执行）") {
                VStack(alignment: .leading, spacing: 10) {
                    ResponsiveRow {
                        TextField("文件夹相对路径前缀", text: $model.searchPathPrefix)
                            .textFieldStyle(.roundedBorder)
                        TextField("开始日期 YYYY-MM-DD", text: $model.searchDateFrom)
                            .textFieldStyle(.roundedBorder).frame(maxWidth: .infinity)
                        TextField("结束日期 YYYY-MM-DD", text: $model.searchDateTo)
                            .textFieldStyle(.roundedBorder).frame(maxWidth: .infinity)
                    }
                    ResponsiveRow {
                        Toggle("只看含 OCR 文字的画面", isOn: $model.searchRequireOCR)
                        Toggle("只看检测到有效人脸的画面", isOn: $model.searchRequirePerson)
                        Spacer()
                    }
                    Text("路径、日期、OCR 与人物条件在读取向量前进入 SQLite 查询，不在界面层过滤。")
                        .font(.caption).foregroundStyle(archiveMuted)
                }.padding(.top, 8)
            }
            DisclosureGroup("当前素材库的搜索历史与保存查询") {
                VStack(alignment: .leading, spacing: 10) {
                    ResponsiveRow {
                        TextField("保存名称", text: $model.savedSearchName)
                            .textFieldStyle(.roundedBorder).frame(maxWidth: 240)
                        Button("保存当前搜索") { model.saveCurrentSearch() }.disabled(true).help("暂不可用：尚无 current-safe 边界")
                            .disabled(model.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        Button("刷新") { model.loadSearchMetadata() }
                        Spacer()
                    }
                    if !model.savedSearches.isEmpty {
                        Text("已保存").font(.caption.bold()).foregroundStyle(archiveMuted)
                        ForEach(model.savedSearches.prefix(8)) { item in
                            Button("\(item.displayName)：\(item.queryText)") {
                                model.applySearchMetadata(item.queryText, filters: item.filters); model.search()
                            }.buttonStyle(.plain).foregroundStyle(archiveBlue)
                        }
                    }
                    if !model.searchHistory.isEmpty {
                        Text("最近搜索").font(.caption.bold()).foregroundStyle(archiveMuted)
                        ForEach(model.searchHistory.prefix(8)) { item in
                            Button("\(item.queryText) · \(item.resultCount) 条 · \(String(format: "%.1f", item.elapsedSeconds)) 秒") {
                                model.applySearchMetadata(item.queryText, filters: item.filters); model.search()
                            }.buttonStyle(.plain).foregroundStyle(.primary)
                        }
                    }
                    if !model.searchMetadataStatus.isEmpty {
                        Text(model.searchMetadataStatus).font(.caption).foregroundStyle(archiveMuted)
                    }
                }.padding(.top, 8)
            }
            DisclosureGroup("人物筛选") {
            ResponsiveRow(spacing: 12) {
                Image(systemName: "person.2.fill").foregroundStyle(archiveBlue)
                if model.personClusterLoading {
                    ProgressView("读取同一人物分组…")
                } else if model.personClusterCatalog.isEmpty {
                    Text("当前没有可确认的多人脸分组").font(.subheadline).foregroundStyle(archiveMuted)
                    Spacer()
                    Button("重新读取") { model.loadPersonClusters() }
                } else {
                    Picker("同一人物", selection: $model.selectedPersonClusterId) {
                        Text("请选择匿名人物").tag("")
                        ForEach(model.personClusterCatalog) { person in
                            Text("\(person.displayName) · \(person.memberCount) 个画面 / \(person.distinctSourceCount) 个素材")
                                .tag(person.personClusterId)
                        }
                    }.frame(maxWidth: 430).onChange(of: model.selectedPersonClusterId) { _ in
                        model.preparePersonEditor()
                    }
                    Button("查看同一人物") {
                        model.searchPersonCluster(model.selectedPersonClusterId)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.selectedPersonClusterId.isEmpty || model.searching)
                    Button("查看侧脸/背影候选") {
                        model.searchPersonTrackSuggestions(model.selectedPersonClusterId)
                    }.disabled(true).help("暂不可用：尚无 current-safe 边界")
                    .buttonStyle(.bordered)
                    .help("只读取同一视频中的现有人脸和人体框；结果需人工确认，不会自动合并人物")
                    .disabled(model.selectedPersonClusterId.isEmpty || model.searching)
                    Spacer()
                }
            }
            }
            if !model.selectedPersonClusterId.isEmpty {
                if let person = model.personClusterCatalog.first(where: { $0.personClusterId == model.selectedPersonClusterId }),
                   let path = person.previewPath, !path.isEmpty {
                    ResponsiveRow {
                        PersonEvidencePreview(path: path, bbox: nil).frame(width: 256, height: 144)
                        Text("当前选择的人物预览。请核对后再确认属于同一人物；人体跟踪不代表身份已确认。")
                            .font(.caption).foregroundStyle(archiveMuted)
                    }
                }
                Divider()
                DisclosureGroup("本地人物名称、标签与人工合并") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("这些修改只保存在本机，不改写机器识别记录；把多个机器组归到同一人物后仍可取消合并。")
                            .font(.caption).foregroundStyle(archiveMuted)
                        ResponsiveRow {
                            TextField("人物名称，例如：张老师", text: $model.personDisplayName)
                                .textFieldStyle(.roundedBorder).frame(maxWidth: 260)
                            TextField("标签，用逗号分隔", text: $model.personTags)
                                .textFieldStyle(.roundedBorder).frame(maxWidth: 300)
                            Button("保存名称和标签") { model.savePersonName() }.disabled(true).help("暂不可用：尚无 current-safe 边界")
                        }
                        ResponsiveRow {
                            Picker("归入", selection: $model.personMergeTargetId) {
                                Text("请选择另一个人物").tag("")
                                ForEach(model.personClusterCatalog.filter { $0.personClusterId != model.selectedPersonClusterId }) { person in
                                    Text(person.displayName).tag(person.personClusterId)
                                }
                            }.frame(maxWidth: 330)
                            Button("确认属于同一人物") { model.mergeSelectedPerson() }.disabled(true).help("暂不可用：尚无 current-safe 边界")
                                .disabled(model.personMergeTargetId.isEmpty)
                            Button("取消该人物的人工合并") { model.detachSelectedPerson() }.disabled(true).help("暂不可用：尚无 current-safe 边界")
                            Spacer()
                        }
                        if !model.personEditStatus.isEmpty {
                            Text(model.personEditStatus).font(.caption).foregroundStyle(archiveBlue)
                        }
                    }.padding(.top, 8)
                }
            }
        } }
        ContentLanguageControl()
        DisclosureGroup("采访与长内容：每个视频说了什么") {
    ResponsiveRow {
        Button("自动识别采访") { model.loadSpokenCatalogue(kind: "likely_interview") }
        Button("长口播 / 长对话") { model.loadSpokenCatalogue(kind: "long_spoken_content") }
        Button("查看全部有转写视频") { model.loadSpokenCatalogue() }
        Button("只看当前人物出现的视频") { model.loadSpokenCatalogue(personOnly: true) }.disabled(model.selectedPersonClusterId.isEmpty)
    }
    Text("当前素材库的已发布转写；疑似采访为规则提示。人物出现在视频中，不代表这个人物就是转写的说话人。").font(.caption)
    if model.spokenScopeIsCurrent && model.spokenLoading { ProgressView("正在读取当前页转写目录…") }
    if model.spokenScopeIsCurrent && !model.spokenError.isEmpty { Text(model.spokenError).foregroundStyle(.red) }
    if let catalog = model.currentSpokenCatalogue {
        Text("共 \(catalog.total) 个视频 · \(catalog.message)").font(.caption).foregroundStyle(archiveMuted)
        if catalog.items.isEmpty { Text("当前范围没有符合条件的已发布转写视频").padding() }
        ForEach(catalog.items) { item in
            VStack(alignment: .leading, spacing: 8) {
                ContentText(item.displayName).font(.headline)
                ContentText("\(item.libraryName) · \(item.contentLabel) · \(videoTimelineTimecode(item.startMs)) — \(videoTimelineTimecode(item.endMs))").font(.caption)
                ContentText(item.excerpt).textSelection(.enabled)
                Text(item.classificationReason).font(.caption).foregroundStyle(archiveMuted)
                ForEach(Array(item.qaClues.enumerated()), id: \.offset) { _, clue in
                    ContentText("疑似问答 \(videoTimelineTimecode(clue.startMs))：\(clue.question) → \(clue.answer)").font(.callout)
                    if let start = clue.answerStartMs, let end = clue.answerEndMs { Text("后续转写范围：\(videoTimelineTimecode(start)) — \(videoTimelineTimecode(end))").font(.caption) }
                    Text(clue.basis).font(.caption).foregroundStyle(archiveMuted)
                }
                Text(item.coverageNote).font(.caption).foregroundStyle(archiveMuted)
                Button("查看原文与时间码") { model.loadVideoContentTimeline(sourceId: item.sourceId, expectedRevision: item.sourceRevision) }
            }.padding(12).frame(maxWidth: .infinity, alignment: .leading).background(Color.gray.opacity(0.06)).cornerRadius(8)
        }
        HStack {
            Button("转写目录上一页") { model.loadSpokenCatalogue(kind: model.spokenKind, personOnly: model.spokenPersonOnly, offset: max(0,catalog.offset-catalog.limit)) }.disabled(catalog.offset == 0 || model.spokenLoading)
            Text("第 \(catalog.offset / catalog.limit + 1) 页")
            Button("转写目录下一页") { model.loadSpokenCatalogue(kind: model.spokenKind, personOnly: model.spokenPersonOnly, offset: catalog.nextOffset ?? 0) }.disabled(catalog.nextOffset == nil || model.spokenLoading)
        }
    }
}

DisclosureGroup("搜索说明与索引状态") {
Text(model.snapshot == nil ? "正在读取所选素材库，请稍候…" : model.snapshot?.searchRuntime.ready == true ? (model.snapshot?.searchRuntime.searchMode == "V2_CURRENT_TEXT" ? "当前结果文字搜索已启用：搜索画面描述、识别文字和人声转写；不加载模型。" : "全量视觉搜索已启用；搜索结果只读，查询向量不持久化；搜索历史和保存查询仅写入当前素材库的本地用户元数据。") : (model.snapshot?.searchRuntime.databasePreflight?.databaseError.flatMap { $0.isEmpty ? nil : "搜索数据库不可用：\($0)" } ?? "单条素材 SEARCH_COMMITTED 后立即开放搜索，无需等待整个项目完成。"))
            .font(.subheadline).foregroundStyle(archiveBlue).padding(12).frame(maxWidth: .infinity, alignment: .leading).background(Color.blue.opacity(0.08)).clipShape(RoundedRectangle(cornerRadius: 8))
        ResponsiveRow(spacing: 7) {
            Image(systemName: model.searchPrewarmReady ? "bolt.fill" : "bolt")
                .foregroundStyle(model.searchPrewarmReady ? archiveGreen : archiveMuted)
            Text(model.snapshot?.searchRuntime.searchMode == "V2_CURRENT_TEXT" ? "当前结果文字搜索无需加载模型" : model.snapshot?.searchRuntime.searchMode == "V2_CURRENT_HYBRID" ? "搜索页面打开且资源充足时，查询模型会保持待命；离开搜索页面或资源不足时，系统按资源保护规则回收。" : model.searchPrewarmStatus).font(.caption).foregroundStyle(archiveMuted)
        }
        Text(model.snapshot?.searchRuntime.searchMode == "V2_CURRENT_TEXT" ? "输入现有描述中的关键词。当前入口不是画面向量语义搜索；图片和视频默认查询画面内容，音频选项查询人声转写。" : (model.searchIntent == "visual" ? "画面内容：使用当前帧的视觉语义及画面描述；对白和邻帧线索不充当直接画面证据。" : model.searchIntent == "text" ? "对白与文字：显示实际原句及时间；预览不代表画面出现对白提到的物体。" : "综合搜索：区分画面、对白、文字及邻帧线索；每个命中时点单独核验。"))
            .font(.caption).foregroundStyle(archiveMuted)
        if !model.personCapabilityNote.isEmpty {
            Label(model.personCapabilityNote, systemImage: "person.crop.circle.badge.questionmark")
                .font(.caption).foregroundStyle(archiveMuted)
        }

}
        if !model.semanticPreparation.isEmpty {Text(model.semanticPreparation).font(.caption).foregroundStyle(archiveMuted)}
        if model.searching, let progress = model.searchProgress {
            Panel { VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Label("正在搜索“\(model.query.trimmingCharacters(in: .whitespacesAndNewlines))”", systemImage: "magnifyingglass")
                        .font(.headline)
                    Spacer()
                    Text(model.selectedSearchLibraries.isEmpty ? "正在查询" : "联合查询 · \(model.selectedSearchLibraries.count) 个库")
                        .font(.caption.bold()).foregroundStyle(archiveBlue)
                    Text(String(format: "%.1f 秒", model.searchElapsedSeconds))
                        .font(.caption.monospacedDigit()).foregroundStyle(archiveMuted)
                    Button(model.searchCancelling ? "正在取消…" : "取消") {
                        model.cancelSearch()
                    }
                    .disabled(model.searchCancelling)
                }
                if model.selectedSearchLibraries.isEmpty {
                    ProgressView().controlSize(.small)
                } else {
                    ProgressView().controlSize(.small)
                    Text("正在逐库查询；完成后显示联合范围统计。不会把当前单库数量当作全部数量。")
                        .font(.caption).foregroundStyle(archiveMuted)
                }
                Text(progress.message).font(.subheadline.bold())
                if let detail = progress.detail, !detail.isEmpty {
                    Text(detail).font(.caption).foregroundStyle(archiveMuted)
                }
                ResponsiveRow(spacing: 18) {
                    if let completed = progress.completed, let total = progress.total, total > 0 {
                        Text("当前进度 \(completed) / \(total)")
                    }
                    if model.selectedSearchLibraries.isEmpty, let overview = model.snapshot?.overview {
                        Text("搜索池：\(overview.visualUnitTotalCount) 个画面 · \(overview.recognition.textVectors) 个文本向量")
                    }
                }.font(.caption).foregroundStyle(archiveMuted)
                if model.searchResults.isEmpty {
                ForEach(0..<1, id: \.self) { _ in
                    ResponsiveRow(spacing: 14) {
                        RoundedRectangle(cornerRadius: 8)
                            .fill(Color.gray.opacity(0.12)).frame(width: 150, height: 82)
                        VStack(alignment: .leading, spacing: 8) {
                            RoundedRectangle(cornerRadius: 4)
                                .fill(Color.gray.opacity(0.14)).frame(maxWidth: .infinity).frame(height: 12)
                            RoundedRectangle(cornerRadius: 4)
                                .fill(Color.gray.opacity(0.10)).frame(maxWidth: .infinity).frame(height: 10)
                            RoundedRectangle(cornerRadius: 4)
                                .fill(Color.gray.opacity(0.10)).frame(maxWidth: .infinity).frame(height: 10)
                        }
                    }
                }
                }
            } }
        }
        if !model.searchStatus.isEmpty { Text(model.searchStatus).font(.subheadline).foregroundStyle(archiveMuted) }
        if !model.snapshotNotice.isEmpty { Text(model.snapshotNotice).font(.caption).foregroundStyle(.secondary) }
        if model.searching {
            Text("本次扫描统计：正在计算当前搜索范围…")
                .font(.caption).foregroundStyle(archiveMuted)
        } else if let coverage = model.searchCoverage {
            Text("本次已扫描：画面向量 \(coverage.scannedVisualVectorCount) / \(coverage.eligibleVisualUnitCount) · 文本向量 \(coverage.scannedTextVectorCount)")
                .font(.caption).foregroundStyle(archiveMuted)
        }
        if !model.searchDiagnostic.isEmpty {
            if !model.searchLibraryNotices.isEmpty {
                DisclosureGroup("已跳过的素材库（\(model.searchLibraryNotices.count)）") {
                    ForEach(model.searchLibraryNotices, id: \.self) { ContentText($0).font(.caption) }
                }
            }
            DisclosureGroup("排查详情", isExpanded: $showSearchDiagnostics) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(model.searchDiagnostic).font(.caption.monospaced()).textSelection(.enabled)
                    Button("复制诊断信息") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(model.searchDiagnostic, forType: .string)
                    }
                }.padding(.top, 8)
            }.padding(12).background(Color.gray.opacity(0.04)).clipShape(RoundedRectangle(cornerRadius: 8))
        }
        if model.searchResults.isEmpty, let overview = model.snapshot?.overview, model.snapshot?.configurationState == "configured" {
            ResponsiveRow { MetricCard(title: "可搜索画面", value: overview.visualUnitTotalCount.formatted()); MetricCard(title: "图片素材", value: (overview.source["image"]?.count ?? 0).formatted()); MetricCard(title: "视频素材", value: (overview.source["video"]?.count ?? 0).formatted()); MetricCard(title: "文本向量", value: overview.recognition.textVectors.formatted()) }
        } else if model.snapshot == nil {
            Panel { VStack { if model.loadError.isEmpty {ProgressView();Text(model.snapshotNotice.isEmpty ? "正在读取所选素材库…" : model.snapshotNotice); if !model.snapshotNotice.isEmpty { Button("重新检查"){model.loadSnapshot()} }} else {Text("素材库读取失败："+model.loadError).foregroundStyle(.red);Button("重试"){model.loadSnapshot()}} }.frame(maxWidth:.infinity).padding(.vertical,48) }
        } else if model.snapshot?.configurationState != "configured" {
            Panel { VStack(spacing: 12) { Image(systemName: "magnifyingglass.circle").font(.system(size: 42)).foregroundStyle(archiveMuted); Text("当前还没有可搜索的素材").font(.title3.bold()); Text("请连接素材库；已提交的素材可立即搜索。").foregroundStyle(archiveMuted) }.frame(maxWidth: .infinity).padding(.vertical, 48) }
        }
        if !model.searchResults.isEmpty {
            LazyVGrid(columns: [GridItem(.flexible(minimum: 0), spacing: 16)], alignment: .leading, spacing: 16) {
                ForEach(model.searchResults) { result in
                    VStack(alignment: .leading, spacing: 8) {
                        if let path = result.libraryTaskPath, resultRequiresLibrarySwitch(path, activePaths: model.snapshot?.existingLibraries.filter { $0.isActive }.map { $0.taskPath } ?? []) {
                            ResponsiveRow {
                                Text("来源素材库：\(result.libraryName ?? path)").font(.headline)
                                Spacer()
                                if let library = model.snapshot?.existingLibraries.first(where: { $0.taskPath == path }) {
                                    Button("进入此库查看画面与人物") { model.activateLibrary(library) }
                                }
                            }
                            Text("联合结果只读展示；进入所属库后可浏览全部画面、确认人物和修改标注。")
                                .font(.caption).foregroundStyle(archiveMuted)
                        }
                        SearchResultCard(result: result).disabled(resultRequiresLibrarySwitch(result.libraryTaskPath, activePaths: model.snapshot?.existingLibraries.filter { $0.isActive }.map { $0.taskPath } ?? [])).onAppear {model.recordSearchCardVisible()}
                    }
                }
            }
        }
        if model.nextSearchOffset != nil {
            ResponsiveRow(spacing: 14) {
                Spacer()
                Button("上一页") { model.search(loadMore: false) }
                    .disabled(model.searching || model.nextSearchOffset == nil)
                Text("每页 30 个素材").font(.caption).foregroundStyle(archiveMuted)
                Button(model.searching ? "加载中…" : "下一页") { model.search(loadMore: true) }
                    .buttonStyle(PrimaryButtonStyle()).disabled(model.searching)
                Spacer()
            }.padding(.vertical, 12)
        }
    }.padding(width < 900 ? 12 : 34).frame(maxWidth: .infinity, alignment: .leading) }.onAppear {
        if model.personClusterCatalog.isEmpty { model.loadPersonClusters() }
        model.prewarmSearch()
    }
    .sheet(isPresented: $model.videoContentSheetPresented) {
        VideoContentTimelineSheet().environmentObject(model)
    }
    }
}

struct FavoritesPage: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                PageHeader(
                    title: "我的收藏",
                    subtitle: "收藏和备注只保存在当前本地素材库；可从收藏或搜索结果勾选画面，导出 PDF 或 Excel 可打开的 CSV。"
                )
                Panel {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Button(model.favoriteLoading ? "读取中…" : "刷新收藏") { model.loadFavorites() }
                                .disabled(model.favoriteLoading)
                            Button("全选当前收藏") { model.selectForExport(model.favoriteResults) }
                                .disabled(model.favoriteResults.isEmpty)
                            Button("清空导出选择") { model.clearExportSelection() }
                                .disabled(model.selectedExportResults.isEmpty)
                            Spacer()
                            Text("已选 \(model.selectedExportResults.count) 项").font(.subheadline.bold())
                            Button("导出 PDF") { model.exportSelectedPDF() }
                                .disabled(model.selectedExportResults.isEmpty)
                            Button("导出 Excel 可打开的 CSV") { model.exportSelectedCSV() }
                                .buttonStyle(PrimaryButtonStyle())
                                .disabled(model.selectedExportResults.isEmpty)
                        }
                        Text("收藏沿用 1.2.0 的素材级记录：同一视频的不同命中画面共享收藏状态；导出选择则精确到当前勾选的命中结果。")
                            .font(.caption).foregroundStyle(archiveMuted)
                        if !model.favoriteStatus.isEmpty { Text(model.favoriteStatus).font(.caption).foregroundStyle(archiveMuted) }
                        if !model.exportStatus.isEmpty { Text(model.exportStatus).font(.caption).foregroundStyle(archiveBlue).textSelection(.enabled) }
                    }
                }
                if model.favoriteResults.isEmpty, !model.favoriteLoading {
                    Panel {
                        VStack(spacing: 10) {
                            Image(systemName: "heart").font(.system(size: 40)).foregroundStyle(archiveMuted)
                            Text("当前素材库还没有收藏").font(.title3.bold())
                            Text("在搜索结果中展开“本地标签、备注与收藏”，勾选收藏并保存后会显示在这里。")
                                .foregroundStyle(archiveMuted)
                        }.frame(maxWidth: .infinity).padding(.vertical, 38)
                    }
                } else {
                    ForEach(model.favoriteResults) { SearchResultCard(result: $0) }
                }
            }.padding(34)
        }.onAppear { model.loadFavorites() }
    }
}

struct DuplicatesPage: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View { ScrollView { VStack(alignment: .leading, spacing: 16) {
        PageHeader(title: "重复素材", subtitle: "这里只标记和建议保留项，绝不会自动删除文件。")
        if model.snapshot?.configurationState != "configured" {
            Panel { Text("完成第一次素材整理后，重复文件组会显示在这里。") }
        } else if let payload = model.snapshot?.duplicateGroups {
            HStack { MetricCard(title: "完全重复组", value: payload.total.formatted(), icon: "square.on.square"); MetricCard(title: "当前显示", value: payload.items.count.formatted(), icon: "list.bullet.rectangle") }
            ForEach(payload.items) { item in Panel { VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("内容完全相同").font(.caption.bold()).foregroundStyle(archiveBlue).padding(7).background(Color.blue.opacity(0.1)).clipShape(RoundedRectangle(cornerRadius: 6))
                    Text("\(item.memberCount ?? item.members.count) 个真实文件").font(.headline)
                    Spacer()
                    Text(formatBytes(item.totalBytes ?? 0)).font(.caption).foregroundStyle(archiveMuted)
                }
                Text("请对照所在文件夹后自行决定保留哪份；软件不会自动删除。标记“建议保留”的只是稳定路径建议。")
                    .font(.caption).foregroundStyle(archiveMuted)
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], alignment: .leading, spacing: 12) {
                    ForEach(Array(item.members.enumerated()), id: \.element.id) { index, member in
                        VStack(alignment: .leading, spacing: 9) {
                            HStack {
                                Text(index == 0 ? "左侧文件" : (index == 1 ? "右侧文件" : "第 \(index + 1) 个文件"))
                                    .font(.caption.bold()).foregroundStyle(archiveBlue)
                                Spacer()
                                if member.isCanonical { Text("建议保留").font(.caption2.bold()).foregroundStyle(archiveGreen) }
                            }
                            Text(member.fileName).font(.subheadline.bold()).lineLimit(2)
                            Text("所在文件夹：\(member.folderPath)")
                                .font(.caption).foregroundStyle(archiveMuted).textSelection(.enabled)
                            Text("文件：\(member.relativePath)")
                                .font(.caption2).foregroundStyle(archiveMuted).textSelection(.enabled)
                            Text("大小：\(formatBytes(member.sizeBytes))").font(.caption2).foregroundStyle(archiveMuted)
                            HStack {
                                Button("打开这个文件夹") { model.openDuplicateFolder(member) }
                                Button("选中这个文件") { model.revealDuplicate(member) }
                            }
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.gray.opacity(0.06))
                        .clipShape(RoundedRectangle(cornerRadius: 9))
                    }
                }
            } } }
        }
    }.padding(34) } }
}

struct SpecialPage: View {
    @EnvironmentObject var model: ArchiveModel
    private func role(_ value: String?) -> String { ["first":"起始帧", "middle":"中间帧", "last":"结束帧"][value ?? ""] ?? "代表帧" }
    var body: some View { ScrollView { VStack(alignment: .leading, spacing: 16) {
        PageHeader(title: "特殊素材", subtitle: "延时摄影按组展示起始、中间和结束代表帧。")
        if model.snapshot?.configurationState != "configured" {
            Panel { Text("完成第一次素材整理后，延时摄影等特殊素材会按组显示在这里。") }
        } else if let payload = model.snapshot?.timelapseGroups {
            HStack { MetricCard(title: "延时摄影组", value: payload.total.formatted(), icon: "timelapse"); MetricCard(title: "连拍", value: "0", icon: "camera.on.rectangle") }
            ForEach(payload.items) { group in Panel { VStack(alignment: .leading, spacing: 13) {
                HStack {
                    Text("延时摄影组 \(group.sequenceId)").font(.headline)
                    Text(group.sourcePhotoCount.map { "整组 \($0.formatted()) 张原始照片" } ?? "整组照片数待确认")
                        .font(.caption.bold()).foregroundStyle(archiveBlue)
                    Spacer()
                    Text("\(group.keyframeCount ?? group.frames.count) 张代表帧").font(.caption).foregroundStyle(archiveMuted)
                    Button("打开原始文件夹") { model.openTimelapseFolder(group) }
                }
                HStack(spacing: 12) { ForEach(group.frames) { frame in VStack(alignment: .leading, spacing: 5) {
                    Button { model.previewTimelapseFrame(frame) } label: {
                        Group { if let path = frame.previewPath, let image = NSImage(contentsOfFile: path) { Image(nsImage: image).resizable().scaledToFill() } else { ZStack { Color.gray.opacity(0.12); Image(systemName: "photo") } } }
                            .frame(width: 210, height: 125).clipped().clipShape(RoundedRectangle(cornerRadius: 8))
                    }.buttonStyle(.plain)
                    Text(role(frame.representativePosition)).font(.caption.bold())
                } } }
                Text(group.sourceRelativeDir ?? group.firstPath ?? "").font(.caption).foregroundStyle(archiveMuted)
            } } }
        }
    }.padding(34) } }
}

struct SettingsPage: View {
    @EnvironmentObject var model: ArchiveModel
    @State private var showYoloeKeywords = false
    @State private var showAdvancedSettings = false
    var body: some View { ScrollView { VStack(alignment: .leading, spacing: 18) {
        PageHeader(title: "处理设置", subtitle: "先看电脑能力，再决定并发、抽帧密度和高价值分析范围。")
        if let state = model.snapshot {
            HStack { MetricCard(title: "芯片", value: state.hardware.chip, tint: archiveBlue, icon: "cpu"); MetricCard(title: "CPU", value: "\(state.hardware.cpuCoresTotal) 核"); MetricCard(title: "GPU", value: state.hardware.gpuCores.map { "\($0) 核" } ?? "未公开"); MetricCard(title: "统一内存", value: state.hardware.unifiedMemoryGb.map { String(format: "%.0f GB", $0) } ?? "未公开") }
            Panel { VStack(alignment: .leading, spacing: 13) {
                Text("处理性能").font(.title3.bold())
                Text("默认值按本机能力保守推荐。遇到内存压力时只能自动降低并发，不会静默提高。").font(.subheadline).foregroundStyle(archiveMuted)
                Divider()
                SettingPicker(title: "处理模式", selection: $model.schedulerMode, values: ["自动选择（推荐）", "自定义（高级）"])
                Text(model.schedulerMode == "自动选择（推荐）"
                     ? "自动模式直接使用当前正式资源与并发策略。"
                     : "高级模式只允许修改当前 backend 支持的安全范围。")
                    .font(.caption).foregroundStyle(archiveBlue)
                DisclosureGroup("高级设置", isExpanded: $showAdvancedSettings) {
                    VStack(alignment: .leading, spacing: 13) {
                        SettingStepper(title: "模型并发路数", value: $model.modelWorkers, range: 1...8, hint: "保守推荐 \(state.hardware.recommendation.modelWorkers) 路 · 估算上限 \(state.hardware.recommendation.estimatedMaxModelWorkers) 路")
                        SettingStepper(title: "抽帧并发路数", value: $model.frameWorkers, range: 1...16, hint: "推荐 \(state.hardware.recommendation.frameExtractWorkers) 路")
                        SettingPicker(title: "视频抽帧间隔", selection: $model.frameInterval, values: ["1 秒", "2 秒", "3 秒", "4 秒", "5 秒"])
                        SettingPicker(title: "高价值分析密度", selection: $model.highValueMode, values: ["兼容当前规则", "目标 15%", "目标 20%", "目标 30%"])
                        SettingPicker(title: "图片分析范围", selection: $model.imageScope, values: ["按当前规则筛选图片", "所有普通图片都进入画面描述"])
                    }
                    .padding(.top, 10)
                }
                Label(
                    state.hasSavedProfile
                        ? "已保存方案会显示在上方，并原样写入下一次新任务。"
                        : "当前尚未保存默认方案；新任务会使用安全默认值。",
                    systemImage: state.hasSavedProfile ? "checkmark.circle.fill" : "info.circle"
                )
                    .font(.caption)
                    .foregroundStyle(state.hasSavedProfile ? archiveGreen : archiveMuted)
                HStack { Spacer(); Button("保存为今后任务的默认方案") { model.saveProfile() }.buttonStyle(PrimaryButtonStyle()) }
            } }
            Panel { VStack(alignment: .leading, spacing: 12) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("YOLOE 识别关键词").font(.title3.bold())
                        Text("A 层默认运行并作为强证据；B 层是可选扩展词，启用后会增加识别范围和计算量。")
                            .font(.subheadline).foregroundStyle(archiveMuted)
                    }
                    Spacer()
                    Button(showYoloeKeywords ? "收起词表" : "编辑词表") {
                        showYoloeKeywords.toggle()
                    }
                }
                Toggle("新任务启用 B 层扩展关键词", isOn: $model.yoloeEnableBExtended)
                    .toggleStyle(.switch)
                Text("当前 A 层 \(model.yoloeACoreText.split(whereSeparator: \.isNewline).count) 个 · B 层 \(model.yoloeBExtendedText.split(whereSeparator: \.isNewline).count) 个。修改只影响今后新建的任务；旧任务继续使用自己的冻结快照。")
                    .font(.caption).foregroundStyle(archiveBlue)
                if showYoloeKeywords {
                    Divider()
                    Text("每行一个关键词，格式为“英文关键词 = 中文说明”；也可以只写英文关键词。重复项保存时自动去重。")
                        .font(.caption).foregroundStyle(archiveMuted)
                    Text("A 层主识别词（至少保留 1 个）").font(.headline)
                    TextEditor(text: $model.yoloeACoreText)
                        .font(.system(.body, design: .monospaced))
                        .frame(minHeight: 190)
                        .padding(6)
                        .background(Color(nsColor: .controlBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.black.opacity(0.12)))
                    Text("B 层扩展辅助词").font(.headline)
                    TextEditor(text: $model.yoloeBExtendedText)
                        .font(.system(.body, design: .monospaced))
                        .frame(minHeight: 190)
                        .padding(6)
                        .background(Color(nsColor: .controlBackgroundColor))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.black.opacity(0.12)))
                    HStack {
                        Button("恢复内置默认词表") { model.restoreDefaultYoloeKeywords() }
                        Spacer()
                        Text("与上方处理方案一起保存").font(.caption).foregroundStyle(archiveMuted)
                    }
                }
            } }
            Panel { VStack(alignment: .leading, spacing: 11) {
                Text("本地模型位置").font(.title3.bold())
                Text("模型不包含在安装包内，也不会由应用联网下载。可放在内置磁盘或外置硬盘。")
                    .font(.subheadline).foregroundStyle(archiveMuted)
                HStack {
                    TextField("选择包含各模型子目录的总目录", text: $model.modelRoot)
                        .textFieldStyle(.roundedBorder)
                    Button("浏览…") { model.chooseModelRoot() }
                    Button("检查并保存") { model.saveModelRoot() }.buttonStyle(PrimaryButtonStyle())
                }
                let items = state.runtimeContract.modelItems ?? []
                Text("已就绪 \(items.filter(\.ready).count) / \(items.count) 项")
                    .font(.subheadline.bold())
                    .foregroundStyle(state.runtimeContract.ready ? archiveGreen : archiveOrange)
                ForEach(items) { item in
                    Label(
                        "\(item.key)：\(item.ready ? "已找到" : "缺失")",
                        systemImage: item.ready ? "checkmark.circle.fill" : "exclamationmark.circle.fill"
                    )
                    .font(.caption)
                    .foregroundStyle(item.ready ? archiveGreen : archiveOrange)
                }
            } }
            Label("抽帧会真实执行 1、2、3、4 或 5 秒合同；高价值密度会按每个视频合格派生帧的 15%、20% 或 30% 动态计算，不写死张数。选择“所有图片”会真实建立补充队列；延时摄影保留代表帧。", systemImage: "checkmark.shield.fill")
                .font(.subheadline).foregroundStyle(archiveMuted).padding(14).frame(maxWidth: .infinity, alignment: .leading).background(Color(nsColor: .controlBackgroundColor).opacity(0.72)).clipShape(RoundedRectangle(cornerRadius: 10))
            Text("系统与安全").font(.title3.bold())
            HStack(alignment: .top, spacing: 12) {
                SafetyCard(title: "中心数据库", detail: state.configurationState == "configured" ? (state.database.integrityCheck == "ok" ? "完整性检查正常，外键错误 \(state.database.foreignKeyErrorCount ?? 0)" : "需要检查") : "首次任务创建后建立", passed: state.database.integrityCheck == "ok")
                SafetyCard(title: "全视觉搜索", detail: state.searchRuntime.ready ? "冻结入口 Stop03-5E V2 可用" : "首次整理完成后开放", passed: state.searchRuntime.ready)
                SafetyCard(title: "原始素材保护", detail: "只读；仅在用户主动打开时访问", passed: true)
            }
            Panel { VStack(alignment: .leading, spacing: 8) { Text("模型更新闸门").font(.headline); Text("登记模型指纹  →  离线小样本测试  →  与当前模型对比  →  人工确认  →  明确启用").font(.subheadline).foregroundStyle(archiveMuted); Text("应用不会联网下载模型，也不会自动替换正式模型。").font(.caption).foregroundStyle(archiveBlue) } }
            if !model.actionMessage.isEmpty { Label(model.actionMessage, systemImage: model.actionFailed ? "xmark.circle" : "checkmark.circle").font(.subheadline).foregroundStyle(model.actionFailed ? Color.red : archiveGreen) }
        } else {
            Panel { VStack(alignment: .leading, spacing: 10) {
                Label("正在读取本机能力与任务状态", systemImage: "gearshape.2")
                if !model.loadError.isEmpty { Text(model.loadError).foregroundStyle(Color.red) }
                if !model.snapshotNotice.isEmpty { Text(model.snapshotNotice).foregroundStyle(.secondary) }
                Text("版本 \(bundledAppVersion) · 设置不会因数据库暂时繁忙而消失。")
                    .font(.caption).foregroundStyle(archiveMuted)
            } }
        }
    }.padding(34) } }
}

struct SettingPicker: View {
    let title: String; @Binding var selection: String; let values: [String]
    var body: some View { HStack { Text(title).frame(width: 160, alignment: .leading); Picker("", selection: $selection) { ForEach(values, id: \.self) { Text($0) } }.labelsHidden().frame(width: 270); Spacer() } }
}
struct SettingStepper: View {
    let title: String; @Binding var value: Int; let range: ClosedRange<Int>; let hint: String
    var body: some View { HStack { Text(title).frame(width: 160, alignment: .leading); Stepper("\(value) 路", value: $value, in: range).frame(width: 130); Text(hint).font(.caption).foregroundStyle(archiveBlue); Spacer() } }
}
struct SafetyCard: View {
    let title: String; let detail: String; let passed: Bool
    var body: some View { Panel { HStack(alignment: .top) { Image(systemName: passed ? "checkmark.circle.fill" : "exclamationmark.circle.fill").font(.title2).foregroundStyle(passed ? archiveGreen : archiveOrange); VStack(alignment: .leading, spacing: 5) { Text(title).font(.headline); Text(detail).font(.caption).foregroundStyle(archiveMuted) } }.frame(maxWidth: .infinity, alignment: .leading) } }
}

// Editorial consumes the frozen portable session and current bridge authority.
struct EditorialCandidate: Decodable, Identifiable {
    let candidateId: String; let sourceId: String; let sourceRevision: String
    let sourceType: String; let displayName: String; let previewPath: String?
    let startMs: Int; let endMs: Int; let score: Double
    let humanStatus: String; let locked: Bool; let manualOrigin: Bool
    let online: Bool; let previewAllowed: Bool?; let resolutionReason: String?
    let matchReasons: [SearchMatchReason]
    var id: String { candidateId }
}
struct EditorialBeat: Decodable, Identifiable {
    let beatId: String; let order: Int; let text: String
    let candidateSnapshots: [EditorialCandidate]
    var id: String { beatId }
}
struct EditorialSession: Decodable {
    let contract: String; let script: String; let beats: [EditorialBeat]
    let activeBeatId: String?; let timelineName: String
}
struct EditorialResponse: Decodable { let session: EditorialSession?; let outputPath: String? }
struct EditorialPreview: Decodable { let originalPath: String; let sourceType: String; let startMs: Int }

extension ArchiveModel {
    func editorialAction(_ command: String, values: [String: Any] = [:]) {
        guard !editorialBusy, !editorialProjectPath.isEmpty else { return }
        editorialBusy = true; editorialStatus = "处理中…"
        var request = values; request["project_path"] = editorialProjectPath
        if request["beat_id"] == nil, let beat = editorialSession?.activeBeatId { request["beat_id"] = beat }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("editorial-\(UUID().uuidString).json")
        do { try JSONSerialization.data(withJSONObject: request).write(to: url, options: .atomic) }
        catch { editorialBusy = false; editorialStatus = error.localizedDescription; return }
        runHelper([command, "--request", url.path]) { data, error in
            try? FileManager.default.removeItem(at: url)
            self.editorialBusy = false
            if let error { self.editorialStatus = error; return }
            guard let data else { self.editorialStatus = "辅助程序没有返回数据"; return }
            do {
                if command == "editorial-preview" {
                    let preview = try self.decoder().decode(EditorialPreview.self, from: data)
                    self.openVideo(path: preview.originalPath, milliseconds: preview.sourceType == "VIDEO" ? preview.startMs : nil)
                } else {
                    let response = try self.decoder().decode(EditorialResponse.self, from: data)
                    if let session = response.session {
                        guard session.contract == "editorial_session_v2" else { throw NSError(domain: "Editorial", code: 1) }
                        self.editorialSession = session; self.editorialScript = session.script
                    }
                }
                self.editorialStatus = "已保存 · 人工决策优先"
            } catch { self.editorialStatus = error.localizedDescription }
        }
    }
    func editorialNew() {
        let panel = NSSavePanel(); panel.nameFieldStringValue = "Editorial.json"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        editorialProjectPath = url.path
        editorialAction("editorial-project-new", values: ["script": editorialScript])
    }
    func editorialOpen() {
        let panel = NSOpenPanel(); panel.allowsMultipleSelection = false; panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        editorialProjectPath = url.path; editorialAction("editorial-project-open")
    }
    func editorialImportScript() {
        let panel = NSOpenPanel(); panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { editorialScript = try String(contentsOf: url, encoding: .utf8) }
        catch { editorialStatus = error.localizedDescription }
    }
    func editorialExport(_ xml: Bool) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = xml ? "Editorial.fcpxml" : "Editorial.manifest.json"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        editorialAction(xml ? "editorial-fcpxml" : "editorial-manifest", values: ["output_path": url.path])
    }
    func editorialAddSearch(_ result: SearchResult) {
        guard editorialSession != nil else { page = .editorial; editorialStatus = "先创建或打开文稿项目，再从搜索/收藏加入素材"; return }
        var payload: [String: Any] = ["source_id": result.sourceId ?? result.sourceContentId ?? "",
                                     "result_id": result.resultId ?? "", "display_name": result.displayName ?? result.sourceRelativePath ?? ""]
        if let cluster = result.matchClusters?.first {
            payload["match_clusters"] = [["cluster_id": cluster.clusterId, "start_time_ms": cluster.startTimeMs,
                "end_time_ms": cluster.endTimeMs, "representative_time_ms": cluster.representativeTimeMs ?? cluster.startTimeMs,
                "representative_frame": ["visual_unit_id": cluster.representativeFrame?.visualUnitId ?? result.visualUnitId ?? "",
                                         "result_id": cluster.representativeFrame?.resultId ?? result.resultId ?? ""]]]
        }
        payload["match_reasons"] = (result.matchReasons ?? []).map { reason in
            ["type": reason.type ?? "METADATA", "authority": reason.authority ?? "Search", "text": reason.text ?? reason.label ?? ""]
        }
        if page == .favorites { payload = ["source_id": result.sourceId ?? result.sourceContentId ?? "", "match_reasons": [["type": "METADATA", "authority": "manual_favorite", "text": "人工收藏"]]] }
        editorialAction("editorial-candidate-manual-add", values: ["search_result": payload]); page = .editorial
    }
}

struct EditorialBoardPage: View {
    @EnvironmentObject var model: ArchiveModel
    @State private var candidateId: String?
    private var beat: EditorialBeat? { model.editorialSession?.beats.first { $0.beatId == model.editorialSession?.activeBeatId } }
    private var candidate: EditorialCandidate? { beat?.candidateSnapshots.first { $0.candidateId == candidateId } }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            PageHeader(title: "文稿选片", subtitle: "已有素材建议 · 人工选择优先 · 时间片不是镜头切分")
            HStack {
                Button("导入文稿") { model.editorialImportScript() }
                Button("新建项目") { model.editorialNew() }.disabled(model.editorialScript.isEmpty)
                Button("打开项目") { model.editorialOpen() }
                Button("保存") { model.editorialAction("editorial-project-save") }
                Button("导出 Manifest") { model.editorialExport(false) }
                Button("导出 FCPXML") { model.editorialExport(true) }
            }.disabled(model.editorialBusy)
            if model.editorialSession == nil {
                TextEditor(text: $model.editorialScript).frame(minHeight: 200)
                Text("输入文稿或导入 UTF-8 文本，再创建 portable JSON 项目。")
            } else {
                HStack(alignment: .top, spacing: 12) {
                    ScrollView { VStack(alignment: .leading, spacing: 8) {
                        ForEach(model.editorialSession?.beats ?? []) { row in
                            Button {
                                candidateId = nil
                                model.editorialAction("editorial-project-save", values: ["active_beat_id": row.beatId])
                            } label: {
                                VStack(alignment: .leading) {
                                    Text("\(row.order). \(row.text)").lineLimit(4)
                                    Text(row.candidateSnapshots.contains { $0.humanStatus == "SELECTED" } ? "已选择" : row.candidateSnapshots.isEmpty ? "未处理" : "需要审核").font(.caption)
                                }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                                    .background(row.beatId == beat?.beatId ? Color.blue.opacity(0.12) : Color.clear)
                            }.buttonStyle(.plain)
                        }
                    }}.frame(width: 220)
                    Divider()
                    VStack(alignment: .leading) {
                        Button("刷新当前段候选") { model.editorialAction("editorial-candidates-refresh") }
                        ScrollView { LazyVStack(alignment: .leading) {
                            ForEach(beat?.candidateSnapshots ?? []) { row in
                                Button { candidateId = row.candidateId } label: {
                                    HStack {
                                        if let path = row.previewPath, let image = NSImage(contentsOfFile: path) {
                                            Image(nsImage: image).resizable().scaledToFit().frame(width: 96, height: 54).background(Color.black.opacity(0.04)).clipped()
                                        } else { Image(systemName: row.sourceType == "VIDEO" ? "film" : "photo").frame(width: 96, height: 54).background(Color.black.opacity(0.04)).clipped() }
                                        VStack(alignment: .leading) {
                                            Text(row.displayName).lineLimit(2)
                                            Text("\(row.sourceType) · \(row.humanStatus)\(row.locked ? " · 锁定" : "")").font(.caption)
                                            Text(String(format: "%.2f–%.2f s · score %.3f", Double(row.startMs)/1000, Double(row.endMs)/1000, row.score)).font(.caption2)
                                            if !row.online { Text("源文件当前不可用").foregroundStyle(.orange) }
                                        }
                                    }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                                        .background(candidateId == row.candidateId ? Color.blue.opacity(0.1) : Color.gray.opacity(0.04))
                                }.buttonStyle(.plain)
                            }
                        }}
                    }.frame(minWidth: 260, maxWidth: .infinity)
                    Divider()
                    if let candidate {
                        EditorialCandidateDetail(candidate: candidate).id(candidate.candidateId).frame(width: 300)
                    } else { Text("选择候选，查看匹配依据、预览和入出点。").frame(width: 300) }
                }
            }
            Text(model.editorialStatus).font(.caption).foregroundStyle(archiveMuted).textSelection(.enabled)
        }.padding(24).disabled(model.editorialBusy)
    }
}
struct EditorialCandidateDetail: View {
    @EnvironmentObject var model: ArchiveModel
    let candidate: EditorialCandidate
    @State private var start = ""; @State private var end = ""
    private func act(_ command: String, _ values: [String: Any]) {
        model.editorialAction(command, values: values.merging(["candidate_id": candidate.candidateId]) { old, _ in old })
    }
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 12) {
            Text(candidate.displayName).font(.headline)
            Text("当前版本：\(candidate.sourceRevision)").font(.caption).textSelection(.enabled)
            Text(candidate.humanStatus + (candidate.manualOrigin ? " · 手动加入" : ""))
            ForEach(candidate.matchReasons) { reason in
                Text("\(reason.type ?? "METADATA") · \(reason.authority ?? "")：\(reason.text ?? reason.label ?? "")").font(.caption)
            }
            if candidate.previewAllowed != true { Text("源文件当前不可用或版本已失效").foregroundStyle(.orange) }
            Button("预览") { act("editorial-preview", [:]) }.disabled(candidate.previewAllowed != true)
            HStack {
                Button("选择") { act("editorial-candidate-decision", ["status": "SELECTED"]) }
                Button("备用") { act("editorial-candidate-decision", ["status": "BACKUP"]) }
                Button("拒绝") { act("editorial-candidate-decision", ["status": "REJECTED"]) }
            }
            TextField("入点（毫秒）", text: $start)
            TextField("出点（毫秒）", text: $end)
            Button("保存入出点") {
                if let a = Int(start), let b = Int(end) { act("editorial-candidate-trim", ["trim": [a,b]]) }
            }
            Button(candidate.locked ? "解锁" : "锁定") { act("editorial-candidate-lock", ["locked": !candidate.locked]) }
            Text("FCPXML 为粗剪；Search proximity cluster 不代表真实镜头边界。").font(.caption).foregroundStyle(archiveMuted)
        }}.onAppear { start = String(candidate.startMs); end = String(candidate.endMs) }
    }
}

func runCheckAndExit() -> Never {
    let helper = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/素材大整理Python")
    let config = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/app_config.json")
    let process = Process(); let output = Pipe(); let errors = Pipe()
    process.environment = phaseCEnvironment()
            process.executableURL = helper; process.arguments = ["--config", config.path, "snapshot"]
    process.standardOutput = output; process.standardError = errors
    do { try process.run(); Build151ChildOwner.register(process) } catch {
        fputs("FAIL helper launch: \(error)\n", stderr); exit(70)
    }
    let data = output.fileHandleForReading.readDataToEndOfFile()
    let errorData = errors.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit(); Build151ChildOwner.finish(process)
    guard process.terminationStatus == 0 else {
        fputs("FAIL helper exit \(process.terminationStatus): \(String(data: errorData, encoding: .utf8) ?? "")\n", stderr)
        exit(process.terminationStatus)
    }
    let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
    do {
        _ = try decoder.decode(Snapshot.self, from: data)
        print("PASS native snapshot decode")
        exit(0)
    } catch {
        fputs("FAIL native snapshot decode: \(error)\n", stderr); exit(65)
    }
}

final class ArchiveTerminationDelegate: NSObject, NSApplicationDelegate {
    let model: ArchiveModel
    private var pendingRequest: UUID?
    var mainWindow: NSWindow?
    init(_ model: ArchiveModel) { self.model = model }
    @objc func showMainWindow(_ sender: Any? = nil) {
        guard let window = mainWindow else { return }
        let wasHidden = !window.isVisible
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if wasHidden {model.searchPageChanged()}
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    func applicationWillTerminate(_ notification:Notification){build151LifecycleEvent("app_exit")}
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if pendingRequest != nil { return .terminateLater }
        let request = UUID()
        pendingRequest = request
        model.prepareToQuit { ready in
            guard self.pendingRequest == request else { return }
            self.pendingRequest = nil
            if !ready { self.showMainWindow() }
            sender.reply(toApplicationShouldTerminate: ready)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 45) {
            guard self.pendingRequest == request else { return }
            self.pendingRequest = nil
            self.model.terminating = false
            self.model.actionFailed = true
            self.model.actionMessage = "安全退出等待超时（quit-handoff 辅助进程）；保留应用，不强制终止。"
            self.showMainWindow()
            sender.reply(toApplicationShouldTerminate: false)
        }
        return .terminateLater
    }
}

struct EditorialReferenceTopic: Identifiable {
    let category: String
    let name: String
    let summary: String
    let looksLike: String
    let editingValue: String
    let documentaryExample: String
    let shortVideoExample: String
    let checklist: [String]
    let avoid: String
    var id: String { category + name }
}

private let editorialReferenceTopics: [EditorialReferenceTopic] = [
    .init(category: "景别", name: "全景：先让观众知道人在哪里", summary: "全景首先解决“这是哪儿、谁在这里、人与环境是什么关系”，不是为了把所有画面都拍得宏大。", looksLike: "人物只占画面一小部分，麦田、村庄、道路或天气成为主要信息。", editingValue: "适合放在一个段落第一次进入新地点时，通常停留 2–5 秒；它像一句话里的主语，先把空间说清楚。", documentaryExample: "旁白说“这次我又站在家里的麦田边”，先给人物站在整片麦田中的全景，随后再切手摸麦穗的近景。观众先认路，再靠近感受。", shortVideoExample: "开头 1 秒先给最有规模感的整片金黄麦田，再迅速切到收割动作和人物表情，用反差建立钩子。", checklist: ["人物和地点是否一眼能看懂？", "这个地点前面是否已经交代过？", "下一镜能否从全景自然靠近到动作或细节？"], avoid: "旁白正在讲迟疑、表情或手部细节时，不要用很远的全景铺满整句；观众会听到情绪，却看不到情绪。"),
    .init(category: "景别", name: "中景：把人物正在做什么说清楚", summary: "中景是“看得清动作，也没丢掉环境”的工作镜头，往往承担一段内容的主体。", looksLike: "人物大约从膝盖、腰部或胸部以上进入画面，同时还能看到他面对的土地、工具或另一人物。", editingValue: "最适合承接完整动作：弯腰、割麦、装袋、交谈、走进田里。它常放在全景之后、特写之前。", documentaryExample: "全景交代麦田后，切家人弯腰收割的中景，让观众看清劳动方式；再切镰刀割断麦秆的特写补证据。", shortVideoExample: "人物说“今天开始收麦”，直接切挥动镰刀的中景，动作落下时再切近景，节奏会比静态说明更有力。", checklist: ["动作有没有清楚的开始和结束？", "人物的视线与行动方向是否和前后镜头一致？", "画面里是否真有事情发生，而不是只有人站着？"], avoid: "连续堆很多相似中景会显得平。若上一镜也是同方向、同大小的人物，应换景别、角度或等待动作变化再切。"),
    .init(category: "景别", name: "近景与特写：把一句话落到证据上", summary: "近景不是“拍得漂亮”，而是告诉观众：这一刻最该看什么。", looksLike: "脸、眼神、手、麦芒、汗水、镰刀或机器部件占据画面主要面积，背景信息被弱化。", editingValue: "适合落关键词、遮盖主镜头删节、放大情绪，也能在长段落中形成视觉重音。通常使用 1–3 秒后就要判断是否该离开。", documentaryExample: "旁白说“风一吹，麦芒就一片一片地晃”，用麦芒被风带动的连续特写；如果画面只是静止麦穗，就只能证明“麦穗”，不能证明“风吹和晃动”。", shortVideoExample: "在“熟了”这个词落下时切一颗饱满麦粒或镰刀划过麦秆的特写，声音同步加强，形成可感知的重音。", checklist: ["特写是否对应旁白中的具体名词或动作？", "前一镜是否已经交代它属于哪里？", "焦点、动作和持续时间是否足够支撑剪点？"], avoid: "没有空间铺垫就连续使用碎特写，观众会看到很多漂亮细节，却不知道这些细节发生在哪里、和谁有关。"),
    .init(category: "构图", name: "三分与视线空间：让画面读起来顺", summary: "三分构图真正有用的地方，是给人物的视线和动作留出方向，不是把主体机械放在格子交点上。", looksLike: "人物偏在一侧，面对或移动的方向留有空间；环境信息能和人物同时被看见。", editingValue: "有助于连接人物看向的对象，也方便字幕避开脸和关键动作。前后镜头的视线方向一致时，切换更自然。", documentaryExample: "人物站在画面左侧望向右边的麦田，下一镜切右侧麦田或正在工作的家人，观众会自然理解“他在看那里”。", shortVideoExample: "人物偏左，右侧留出标题或关键词；随后让人物向右移动，带出下一个画面。", checklist: ["留白是否在人物看向或移动的方向？", "字幕会不会挡住脸、手或关键物件？", "下一镜是否回答了人物视线提出的问题？"], avoid: "人物朝左看却把空间全留在右边，或为了三分法把关键动作挤到边缘，会让画面别扭。"),
    .init(category: "构图", name: "居中与对称：明确强调，而不是默认摆正", summary: "居中会让观众立刻注意主体，适合强调、仪式感和段落句号；越整齐，态度越明确。", looksLike: "人物、道路、建筑或机器位于画面中心，左右结构相对平衡。", editingValue: "适合开头钩子、人物正面陈述、机械迎面工作或段落收束。和偏置构图交替时，能产生明显节奏变化。", documentaryExample: "收割机从麦田中央正面驶来，可以表现机械进入传统劳动现场的冲击；随后切侧面中景恢复观察距离。", shortVideoExample: "开头让人物正对镜头居中说出冲突句，下一拍快速切现场证据，强调会更直接。", checklist: ["居中是在强调什么？", "画面左右是否真的形成秩序？", "前后镜头有没有构图变化，避免一直像证件照？"], avoid: "没有强调目的时把所有人物都放正中，会显得呆板；背景歪斜或杂乱时，居中反而会放大问题。"),
    .init(category: "构图", name: "前景与纵深：让观众感觉自己在现场", summary: "前景不是装饰，它可以交代摄影机站在哪里，并把近、中、远三个空间组织起来。", looksLike: "近处麦穗或门框形成遮挡，中间有人劳动，远处还有树林、房屋或另一组人物。", editingValue: "适合观察式纪录片、人物进入空间和从局部揭示整体。画面层次丰富时，可以比普通全景多停一会儿。", documentaryExample: "隔着前景麦穗拍家人收割，会产生“我站在田边看着”的主观位置，比悬空的漂亮航拍更接近回家观察的语气。", shortVideoExample: "先用前景遮住主体，再通过小幅移动露出收割现场，天然形成一次视觉揭示。", checklist: ["前景有没有帮助交代观看位置？", "关键人物和动作是否仍然清楚？", "前中后景之间是否有可读的层次，而不是杂乱堆叠？"], avoid: "前景挡住脸、手或动作关键点时，它不再增加现场感，只是在妨碍信息。"),
    .init(category: "构图", name: "负空间：把迟疑和等待留在画面里", summary: "主体周围的大块空白，会让观众感到距离、等待、不确定，也能为字幕和下一动作留下位置。", looksLike: "人物很小或偏在一边，天空、空地、墙面、未收割的麦田占据较大面积。", editingValue: "适合疑问、停顿、人物没有马上回答的时刻。它允许镜头多呼吸半秒，而不是急着用信息填满。", documentaryExample: "旁白问“这麦子还是我家的吗”，人物站在田边、身旁留出大片麦田，比立刻切热闹收割更能保留问题。", shortVideoExample: "冲突句之后留一个短暂停顿和空画面，再切答案或反转；空白本身构成节奏。", checklist: ["空白是否服务于迟疑、距离或字幕？", "主体仍能被观众迅速找到吗？", "声音是否足以支撑这段停留？"], avoid: "事实说明和快速教程里长时间留白，会被理解成信息不足或剪辑拖沓。"),
    .init(category: "角度", name: "平视：让观众和人物站在同一高度", summary: "平视通常最少替观众下判断，适合日常劳动、交谈和长期观察。", looksLike: "摄影机高度接近人物眼睛、胸口或正在工作的手，不明显俯视或仰视。", editingValue: "作为段落主体最稳妥，能让人物自己通过动作和声音表达，而不是靠角度制造强弱。", documentaryExample: "和弯腰割麦的人保持相近高度，观众更像站在田里一起看，而不是从高处检查一项劳动。", shortVideoExample: "人物直接对镜口播时使用平视，可信度通常比夸张低机位更高；再用特写加强重点。", checklist: ["机位高度是否符合人物当时的状态？", "这个角度有没有无意中居高临下？", "背景是否因为平视而过于杂乱？"], avoid: "平视不等于客观。如果距离过近、剪点偏向某一方，仍然会表达明显态度。"),
    .init(category: "角度", name: "俯拍与仰拍：先明确你为什么改变高度", summary: "高低角度首先改变空间信息和体量感，不能简单套成“俯拍弱、仰拍强”。", looksLike: "俯拍能看到地面、队形和劳动范围；仰拍会让人物、麦穗或机器压向天空，体量更突出。", editingValue: "俯拍适合解释多人如何分布、田块有多大；仰拍适合表现机器、成长或主观压迫。角度变化应有叙事动机。", documentaryExample: "从高处看几个人分散收麦，是在解释劳动组织；从低处拍麦穗遮住人物，则可能强调土地压过人的存在感。", shortVideoExample: "低机位让收割机迎面驶过作为强钩子，随后切俯拍展示它实际覆盖的面积，完成“冲击—解释”。", checklist: ["这个角度增加了什么新信息？", "它是否把人物塑造成了并非本意的强或弱？", "和前后镜头切换时，空间方向是否还能理解？"], avoid: "只因为角度“特别”就使用，会让纪录片态度显得用力过猛，也可能破坏真实关系。"),
    .init(category: "运镜", name: "固定镜头：让事情自己发生", summary: "固定不是“什么都不做”，而是把注意力交给画内动作、时间和声音。", looksLike: "画框基本不变，人物走进走出、风吹麦浪、机器经过或动作完整发生。", editingValue: "适合保留完整劳动过程、建立观察感和给快节奏段落留呼吸。好用的固定镜头内部必须有变化。", documentaryExample: "固定拍一束麦子从站立到被割倒，比来回追着镰刀晃动更能让观众看清动作全过程。", shortVideoExample: "连续快切后突然停在一个稳定画面 1–2 秒，能制造重音；画内动作仍要在这一秒内发生。", checklist: ["画内有没有明确动作或状态变化？", "镜头开始和结束是否干净？", "同期声能否帮助观众继续看下去？"], avoid: "代表帧看起来稳定，不等于整段固定；必须播放原片确认有没有抖动、重新构图或无意义等待。"),
    .init(category: "运镜", name: "跟拍：让观众和人物一起行动", summary: "跟拍的价值是维持人物行动和空间连续，不是单纯让画面更“动”。", looksLike: "摄影机随人物走进田里、跟随搬运麦捆，人物在画面中的大小相对稳定，背景不断变化。", editingValue: "适合人物从一个地点到另一个地点、带观众进入现场，也可以覆盖一段连续旁白。", documentaryExample: "从家门口跟到麦田，让“人该回去了”变成真实路程；如果只给麦田空镜，回去这个动作并没有发生。", shortVideoExample: "人物边走边说，镜头跟进，到动作点快速切目的地特写，能保持向前推进。", checklist: ["人物行动是否有明确目的地？", "跟拍方向能否接上下一个镜头？", "抖动和对焦是否在可接受范围？"], avoid: "人物没有移动目标、摄影机却不停漂移，会抢走内容注意力；单帧无法判断跟拍质量。"),
    .init(category: "运镜", name: "推、拉、摇、移：每次运动只回答一个问题", summary: "推近是在强调，拉远是在揭示关系，摇移是在把两个信息连接起来；运动应有明确起点和落点。", looksLike: "从环境推到人物或细节、从人物拉出整个场地、从麦穗摇到劳动者、横移展示一排作业。", editingValue: "运动完成时天然形成句号和剪点。最好把旁白关键词落在画面刚刚揭示目标的时刻。", documentaryExample: "从家人手里的麦穗慢慢拉出整片田，能把“这一把麦子”和“这块地”联系起来；中途停不稳则失去意义。", shortVideoExample: "快速推近细节配合音效做钩子，但下一镜要提供新信息，不能只重复放大。", checklist: ["运动从什么开始、最终让观众看见什么？", "关键词是否能落在揭示完成的时刻？", "速度是否符合这一段的情绪和节奏？"], avoid: "没有起落点的随意摇晃、为了转场而甩镜，会让后期难以找到干净出入点。"),
    .init(category: "剪辑", name: "一组镜头怎么搭：空间—动作—细节—反应", summary: "不是每句话配一张图，而是用不同镜头共同完成一个小段落。", looksLike: "先交代地点，再看人物做事，再看关键细节，最后看人物或环境产生什么反应。", editingValue: "这套顺序能避免观众迷路，也避免一段旁白从头到尾只用同一种麦田画面。根据内容可以跳过其中任何一步。", documentaryExample: "麦田全景 → 家人收割中景 → 镰刀和麦秆特写 → 人停下来擦汗或风吹空田。四个镜头共同说明“收麦正在发生以及它给人的感受”。", shortVideoExample: "先用最强动作特写做钩子，再补全景解释地点，然后回中景推进动作；网感视频可以改变顺序，但仍要把空间和事实补回来。", checklist: ["观众是否知道在哪里、谁在做什么？", "每一镜是否提供了上一镜没有的新信息？", "最后一镜是否能把段落送入下一句？"], avoid: "把它当固定模板会机械。若一个长镜头已经完整交代动作，就不必为了凑四种角色硬切。"),
    .init(category: "剪辑", name: "动作剪接：在动作还没结束时切", summary: "利用同一个动作连接两个镜头，观众会跟着动作走，而不是注意到剪切本身。", looksLike: "中景里手开始挥镰，切到特写时镰刀继续落下；人物转身过程中切到另一角度，动作方向保持。", editingValue: "可以缩短过程、换景别并隐藏剪点。真正的入点和出点必须回到原片逐帧确认。", documentaryExample: "中景看到人伸手抓麦秆，在手刚碰到麦秆时切手部特写，既连续又把重点落到劳动细节。", shortVideoExample: "把起身、拿工具、走出门三个动作的中段连接起来，可以快速压缩时间而不显杂乱。", checklist: ["两个镜头是否处在同一动作阶段？", "人物、工具和移动方向有没有突然反转？", "切点前后是否各留了足够帧数？"], avoid: "动作已经结束后才切，容易顿一下；两个镜头动作幅度或手的位置对不上，会产生明显跳动。"),
    .init(category: "剪辑", name: "插入与反应：补证据，不做空泛配图", summary: "插入镜头回答“具体是什么”，反应镜头回答“这件事对人或现场造成了什么”。", looksLike: "账本、老照片、麦粒、工具属于插入；人物停顿、互看、擦汗、空田恢复安静属于反应。", editingValue: "能补足抽象旁白、遮盖主镜头删节，也能让事件不只剩动作。", documentaryExample: "说“以前知道谁种谁收”时，可用具体劳动者、工具或旧物作证；随便一片漂亮麦田只能相关，不能证明“谁”。", shortVideoExample: "观点句后切观众表情或结果画面，形成反应；若没有真实反应，宁可保留口播，不要伪造情绪。", checklist: ["它在补哪一个具体信息？", "画面能够证明旁白到什么程度？", "拿掉它之后，段落是否会缺证据或缺情绪变化？"], avoid: "只因为关键词相同就插入，会形成“说麦子就永远放麦田”的机械配图。"),
    .init(category: "声音", name: "环境声：让画面不只是无声说明图", summary: "风、脚步、镰刀、机器和远处说话声，会告诉观众空间大小、距离和现场是否真实。", looksLike: "它不一定有对白，但能听到与画面一致的声源和远近层次。", editingValue: "环境声可以撑住空镜、连接不同景别，也能在旁白停顿时保留现场。先处理清楚人声，再决定环境声有多响。", documentaryExample: "旁白停在“我也说不好”之后，留半秒风吹麦穗和远处机器声，比马上塞入下一句更能让疑问落地。", shortVideoExample: "开头先听到收割机声再看到机器，声音先行能制造期待；随后快速进入人物解释。", checklist: ["声源和当前画面是否对得上？", "旁白停顿处有没有可用的现场声？", "切换镜头时底噪是否突然跳变？"], avoid: "把所有环境声降到没有，纪录片会像配图朗读；底噪差异过大时硬切，也会暴露剪点。"),
    .init(category: "声音", name: "J-cut / L-cut：让声音先走一步或多留一步", summary: "下一场声音先出现叫 J-cut；上一场声音跨过画面切换继续存在叫 L-cut。它们让切换不那么生硬。", looksLike: "还在看村庄时先听见收割机；画面已经切到麦田，上一镜家人的一句话仍然说完。", editingValue: "适合空间转换、采访配 B-roll 和旁白段落衔接。声音先后关系能引导观众，而不需要花哨转场。", documentaryExample: "先听见家人说“快收了”，再切到他说话的人或麦田；声音把熟悉感带进画面。", shortVideoExample: "下一镜的动作声提前 3–8 帧进入，能把连续快切粘在一起，节奏会更紧。", checklist: ["声音提前或延后是在引导什么？", "跨镜头后声源关系仍然合理吗？", "对白是否被画面切换抢走注意力？"], avoid: "没有空间或叙事联系时随意跨声音，会让观众误以为声源仍在当前画面。"),
    .init(category: "色彩", name: "LOG 素材：先判断能不能用，再谈风格", summary: "LOG 的灰、淡、低对比只是拍摄记录方式，不代表最终情绪，更不能直接判定素材不好看。", looksLike: "黑不够黑、颜色偏淡、皮肤和麦田都像蒙了一层灰，但高光和阴影可能保留了更多信息。", editingValue: "选片阶段重点看曝光是否可救、主体是否清楚、镜头内容和剪辑作用是否成立；风格统一留到调色阶段。", documentaryExample: "同一场麦田的 A7M4、无人机和手机颜色不同，粗剪先按内容与时间连接，之后再统一白平衡、对比和麦田黄色。", shortVideoExample: "不要因为手机素材看起来更鲜艳就优先选它；先选动作最强、构图最清楚的镜头，再做统一调色。", checklist: ["高光有没有死白、阴影有没有彻底丢失？", "人物肤色和麦田是否有可分离的信息？", "不同机位是否属于同一时间和光线条件？"], avoid: "把未调色的灰理解成忧郁、把偏黄理解成温暖，都是在素材阶段过早赋予情绪。"),
    .init(category: "色彩", name: "冷暖与明暗：用变化服务段落，而不是套滤镜", summary: "色彩最有用的时候，是帮助观众区分时间、空间和情绪变化；同一场景首先要连续。", looksLike: "清晨偏冷、傍晚偏暖，室内外亮度不同，人物与背景可能靠明暗或颜色分开。", editingValue: "同一段先保证白平衡和曝光连续；真正的冷暖变化最好落在叙事转折，而不是每个镜头各有一种滤镜。", documentaryExample: "回忆段落不必强行变黄。若旧照片、环境声和叙事已经说明过去，保持克制通常比复古滤镜更可信。", shortVideoExample: "开头可用更明确的明暗反差抓眼，但肤色、产品或真实现场不能为了“网感”失真。", checklist: ["相邻镜头的白平衡是否突然跳变？", "主体和背景是否有足够分离？", "冷暖变化是否对应真实时间或叙事转折？"], avoid: "只靠高饱和、高对比制造网感，会让麦田细节、肤色和纪录片可信度一起受损。"),
]
struct EditorialReferencePage: View {
    @State private var category = "景别"
    @State private var selectedTopicId = editorialReferenceTopics[0].id
    private let categories = ["景别", "构图", "角度", "运镜", "剪辑", "声音", "色彩"]
    private var categoryTopics: [EditorialReferenceTopic] {
        editorialReferenceTopics.filter { $0.category == category }
    }
    private var selectedTopic: EditorialReferenceTopic {
        categoryTopics.first(where: { $0.id == selectedTopicId }) ?? categoryTopics[0]
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                PageHeader(title: "剪辑参考：先看懂，再决定用不用", subtitle: "这里不背术语。每一项都回答：画面长什么样、能帮这句话做什么、什么时候反而不该用。")
                Panel {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("挑一个镜头前，先问三个问题").font(.title3.bold())
                        HStack(alignment: .top, spacing: 12) {
                            referenceQuestion("1", "这句话需要什么？", "是交代地点、证明动作、看人物反应，还是只需要一次呼吸？")
                            referenceQuestion("2", "画面真的能证明吗？", "“有麦田”不能自动证明“这是我家”，静止麦穗也不能证明“风正在吹”。")
                            referenceQuestion("3", "它能接上前后镜头吗？", "检查景别、方向、动作、声音和节奏；相关不等于剪辑上可用。")
                        }
                    }
                }
                Picker("类别", selection: $category) { ForEach(categories, id: \.self) { Text($0) } }
                    .pickerStyle(.segmented)
                HStack(alignment: .top, spacing: 18) {
                    Panel {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("选择一个问题").font(.headline)
                            ForEach(categoryTopics) { topic in
                                Button { selectedTopicId = topic.id } label: {
                                    HStack {
                                        Text(topic.name).multilineTextAlignment(.leading)
                                        Spacer(); Image(systemName: "chevron.right").font(.caption)
                                    }.padding(10)
                                    .background(selectedTopicId == topic.id ? Color.blue.opacity(0.12) : Color.clear)
                                    .foregroundStyle(selectedTopicId == topic.id ? archiveBlue : Color.primary)
                                    .clipShape(RoundedRectangle(cornerRadius: 8))
                                }.buttonStyle(.plain)
                            }
                        }.frame(width: 245, alignment: .leading)
                    }
                    topicDetail(selectedTopic).frame(maxWidth: .infinity)
                }
                Panel {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "exclamationmark.shield.fill").foregroundStyle(archiveOrange)
                        Text("判断边界：抽样帧可以初步看景别、构图和角度；运镜、动作起落、稳定性、声音和真实入出点必须播放原片。LOG 画面未经调色，不能根据当前灰淡外观决定最终情绪。")
                            .font(.callout)
                    }
                }
            }.padding(34)
        }.onChange(of: category) { newCategory in
            if let first = editorialReferenceTopics.first(where: { $0.category == newCategory }) {
                selectedTopicId = first.id
            }
        }
    }

    private func referenceQuestion(_ number: String, _ title: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack { Text(number).font(.caption.bold()).foregroundStyle(.white).frame(width: 22, height: 22).background(Circle().fill(archiveBlue)); Text(title).font(.headline) }
            Text(detail).font(.callout).foregroundStyle(archiveMuted).fixedSize(horizontal: false, vertical: true)
        }.padding(12).frame(maxWidth: .infinity, alignment: .topLeading)
            .background(Color.blue.opacity(0.05)).clipShape(RoundedRectangle(cornerRadius: 9))
    }

    private func topicDetail(_ topic: EditorialReferenceTopic) -> some View {
        Panel {
            VStack(alignment: .leading, spacing: 16) {
                HStack { Text(topic.name).font(.system(size: 25, weight: .bold)); Spacer(); Text(topic.category).font(.caption.bold()).foregroundStyle(archiveBlue).padding(.horizontal, 9).padding(.vertical, 4).background(Color.blue.opacity(0.1)).clipShape(Capsule()) }
                Text(topic.summary).font(.title3).foregroundStyle(archiveBlue).fixedSize(horizontal: false, vertical: true)
                EditorialReferenceVisual(topic: topic)
                referenceSection("眼前会看到什么", "eye", topic.looksLike, archiveBlue)
                referenceSection("放进剪辑里能做什么", "scissors", topic.editingValue, archiveGreen)
                HStack(alignment: .top, spacing: 12) {
                    exampleCard("纪录片例子", "film", topic.documentaryExample)
                    exampleCard("网感视频例子", "bolt.fill", topic.shortVideoExample)
                }
                VStack(alignment: .leading, spacing: 7) {
                    Label("选片时逐项确认", systemImage: "checklist").font(.headline)
                    ForEach(topic.checklist, id: \.self) { row in
                        HStack(alignment: .top) { Image(systemName: "square").font(.caption).foregroundStyle(archiveBlue).padding(.top, 3); Text(row) }
                    }
                }
                VStack(alignment: .leading, spacing: 6) {
                    Label("什么时候别用", systemImage: "exclamationmark.triangle.fill").font(.headline).foregroundStyle(archiveOrange)
                    Text(topic.avoid).fixedSize(horizontal: false, vertical: true)
                }.padding(12).background(Color.orange.opacity(0.08)).clipShape(RoundedRectangle(cornerRadius: 9))
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func referenceSection(_ title: String, _ icon: String, _ text: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(title, systemImage: icon).font(.headline).foregroundStyle(color)
            Text(text).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func exampleCard(_ title: String, _ icon: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Label(title, systemImage: icon).font(.headline)
            Text(detail).font(.callout).fixedSize(horizontal: false, vertical: true)
        }.padding(13).frame(maxWidth: .infinity, alignment: .topLeading)
            .background(Color.gray.opacity(0.06)).clipShape(RoundedRectangle(cornerRadius: 9))
    }
}

private struct EditorialReferenceVisual: View {
    let topic: EditorialReferenceTopic
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var assetName: String? {
        switch topic.category {
        case "景别": return "shot_sizes_16x9"
        case "构图": return "composition_16x9"
        case "角度": return "camera_angles_16x9"
        case "运镜": return "camera_motion_16x9"
        default: return nil
        }
    }

    private var labels: [String] {
        switch topic.category {
        case "景别": return ["远景 / 全景", "中景", "近景", "特写"]
        case "构图": return ["三分与视线空间", "居中与对称", "前景与纵深", "负空间"]
        case "角度": return ["平视", "俯拍", "仰拍", "顶视 / 空间关系"]
        case "运镜": return ["固定", "跟拍", "推近 / 拉远", "摇移 / 揭示"]
        default: return []
        }
    }

    private var activeIndexes: Set<Int> {
        let name = topic.name
        if name.contains("全景") { return [0] }
        if name.contains("中景") { return [1] }
        if name.contains("近景与特写") { return [2, 3] }
        if name.contains("三分") { return [0] }
        if name.contains("居中") { return [1] }
        if name.contains("前景") { return [2] }
        if name.contains("负空间") { return [3] }
        if name.contains("平视") { return [0] }
        if name.contains("俯拍与仰拍") { return [1, 2] }
        if name.contains("固定镜头") { return [0] }
        if name.contains("跟拍") { return [1] }
        if name.contains("推、拉、摇、移") { return [2, 3] }
        return []
    }

    private var image: NSImage? {
        guard let assetName,
              let path = Bundle.main.path(
                forResource: assetName,
                ofType: "png",
                inDirectory: "media_archive_image_video_ui/assets/editorial_reference"
              ) else { return nil }
        return NSImage(contentsOfFile: path)
    }

    var body: some View {
        if let image {
            VStack(alignment: .leading, spacing: 8) {
                ZStack(alignment: .topLeading) {
                    Image(nsImage: image)
                        .resizable()
                        .scaledToFill()
                        .aspectRatio(16.0 / 9.0, contentMode: .fit)
                        .frame(maxWidth: .infinity)
                        .clipped()
                    if topic.category == "运镜" {
                        motionOverlay
                    }
                    gridHighlight
                    Text(topic.category == "运镜" ? "16:9 动态示意 · 每格均为横版" : "16:9 画面对照 · 每格均为横版")
                        .font(.caption2.bold()).foregroundStyle(.white)
                        .padding(.horizontal, 9).padding(.vertical, 5)
                        .background(.black.opacity(0.62)).clipShape(Capsule())
                        .padding(10)
                }
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.black.opacity(0.10)))
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 6) {
                    ForEach(Array(labels.enumerated()), id: \.offset) { index, label in
                        Text(label).font(.caption2.bold()).lineLimit(1).minimumScaleFactor(0.75)
                            .foregroundStyle(activeIndexes.contains(index) ? Color(nsColor: .controlBackgroundColor) : Color.primary)
                            .padding(.horizontal, 7).padding(.vertical, 5)
                            .frame(maxWidth: .infinity)
                            .background(activeIndexes.contains(index) ? archiveBlue : Color.gray.opacity(0.10))
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                }
                Text("蓝色标签是当前条目。对照图只帮助理解画面差异，真实运镜、动作和声音仍需播放原片确认。")
                    .font(.caption2).foregroundStyle(archiveMuted)
            }
        }
    }

    private var gridHighlight: some View {
        GeometryReader { proxy in
            ForEach(activeIndexes.sorted(), id: \.self) { index in
                RoundedRectangle(cornerRadius: 5)
                    .stroke(archiveBlue, lineWidth: 4)
                    .frame(width: proxy.size.width / 2, height: proxy.size.height / 2)
                    .offset(
                        x: index.isMultiple(of: 2) ? 0 : proxy.size.width / 2,
                        y: index < 2 ? 0 : proxy.size.height / 2
                    )
            }
        }.allowsHitTesting(false)
    }

    @ViewBuilder private var motionOverlay: some View {
        if reduceMotion {
            motionMarker(progress: 0.55)
        } else {
            TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
                let progress = context.date.timeIntervalSinceReferenceDate
                    .truncatingRemainder(dividingBy: 2.4) / 2.4
                motionMarker(progress: progress)
            }
        }
    }

    private func motionMarker(progress: Double) -> some View {
        GeometryReader { proxy in
            let usable = max(0, proxy.size.width - 58)
            HStack(spacing: 4) {
                Image(systemName: "video.fill").accessibilityHidden(true)
                Image(systemName: "arrow.right").accessibilityHidden(true)
            }
            .font(.caption.bold()).foregroundStyle(.white)
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(archiveBlue.opacity(0.88)).clipShape(Capsule())
            .offset(x: 8 + usable * progress, y: max(8, proxy.size.height - 40))
        }.allowsHitTesting(false)
    }
}

// Phase C native surfaces over frozen build150 boundaries. No processing owner.
private let phaseCUnavailable = "暂不可用：当前稳定产品边界尚未提供"
private var phaseCStateRoot: String {
    ProcessInfo.processInfo.environment["PHASE_C_STATE_ROOT"] ??
    (NSHomeDirectory() + "/Library/Application Support/素材大整理/1.3.2-build151")
}
private let build151IntegritySession = UUID().uuidString
private func phaseCEnvironment() -> [String:String] {
    var e = ProcessInfo.processInfo.environment
    e["MEDIA_ARCHIVE_APP_STATE_ROOT"] = phaseCStateRoot
    e["BUILD151_UI_SESSION"] = build151IntegritySession
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    e["HF_HUB_OFFLINE"] = "1"; e["TRANSFORMERS_OFFLINE"] = "1"
    return e
}
private final class PhaseCReadModel: ObservableObject {
    @Published var facts: [String:Any] = [:]
    @Published var error = ""
    private var busy = false
    func refresh() {
        guard !busy && !Build151ChildOwner.closing else { return }; busy = true
        let contents = Bundle.main.bundleURL.appendingPathComponent("Contents")
        DispatchQueue.global(qos: .utility).async {
            let p = Process(); let out = Pipe(); let err = Pipe()
            p.executableURL = contents.appendingPathComponent("Helpers/PipelinePython/system")
            p.arguments = [contents.appendingPathComponent("Resources/phasec_view_api.py").path,"footer"]
            p.environment = phaseCEnvironment(); p.standardOutput = out; p.standardError = err
            do {
                try p.run(); Build151ChildOwner.register(p); let data = out.fileHandleForReading.readDataToEndOfFile()
                let errors = err.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit(); Build151ChildOwner.finish(p)
                let facts = (try JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]
                DispatchQueue.main.async {
                    self.facts = facts; self.error = p.terminationStatus == 0 ? "" : (facts["error"] as? String ?? String(data:errors,encoding:.utf8) ?? "读取失败")
                    self.busy = false
                }
            } catch { DispatchQueue.main.async { self.error = error.localizedDescription; self.busy = false } }
        }
    }
    func number(_ key: String) -> String { (facts[key] as? NSNumber)?.stringValue ?? "—" }
    var folders: [[String:Any]] { facts["folders"] as? [[String:Any]] ?? [] }
    var stages: [[String:Any]] { facts["stages"] as? [[String:Any]] ?? [] }
    func stage(_ name: String, _ status: String) -> String {
        let rows = stages.filter { $0["stage"] as? String == name && $0["status"] as? String == status }
        return rows.isEmpty ? "—" : String(rows.compactMap{$0["count"] as? Int}.reduce(0,+))
    }
}

private struct PhaseCBox<Content:View>: View {
    var title: String; @ViewBuilder var content: () -> Content
    var body: some View { VStack(alignment:.leading,spacing:12) {
        if !title.isEmpty { Text(title).font(.headline) }; content()
    }.padding(16).frame(maxWidth:.infinity,alignment:.leading)
        .background(Color(nsColor:.controlBackgroundColor)).overlay(RoundedRectangle(cornerRadius:10).stroke(Color.primary.opacity(0.09))).clipShape(RoundedRectangle(cornerRadius:10)) }
}
private struct PhaseCDisabled: View {
    var title: String; var reason: String = phaseCUnavailable
    var body: some View { VStack(alignment:.leading,spacing:5) { Button(title) {}.disabled(true); Text(reason).font(.caption).foregroundStyle(.secondary) } }
}
private struct PhaseCShell: View {
    @EnvironmentObject var model: ArchiveModel
    @StateObject private var status = PhaseCReadModel()
    @AppStorage("layout.sidebarVisible") private var expanded = true
    @State private var darkOverride: Bool? = nil
    private let timer = Timer.publish(every:5,on:.main,in:.common).autoconnect()
    var body: some View {
        GeometryReader { geometry in
        let compact = geometry.size.width < 1100
        VStack(spacing: 0) {
            if compact {
                HStack {
                    Menu { ForEach(ArchivePage.allCases) { page in
                        Button { model.page = page } label: { Label(page.rawValue, systemImage: page.icon) }
                    } } label: { Label("导航", systemImage: "sidebar.left") }
                    .accessibilityIdentifier("responsive.navigation")
                    Text(model.page.rawValue).font(.headline)
                    Spacer(minLength: 0)
                    Text("build" + (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "—")).font(.caption).foregroundStyle(.secondary)
                }.padding(.horizontal, 12).padding(.vertical, 6)
                Divider()
            }
        HStack(spacing:0) {
            if !compact {
            ScrollView(.vertical) {

            VStack(alignment:expanded ? .leading : .center,spacing:12) {
                Button { expanded.toggle() } label: { Image(systemName:"sidebar.left") }.help(expanded ? "收起侧栏" : "展开侧栏").accessibilityIdentifier("phasec.sidebar.toggle")
                HStack { Image(nsImage:NSApplication.shared.applicationIconImage).resizable().scaledToFit().frame(width:36,height:36)
                    if expanded { VStack(alignment:.leading) { Text("本地数据库").bold(); Text("本地素材整理与搜索").font(.system(size:9)).foregroundStyle(.secondary) } }
                }.padding(.bottom,8)
                ForEach(ArchivePage.allCases) { page in
                    Button { model.page = page } label: {
                        HStack(spacing:10) { Image(systemName:page.icon).frame(width:18); if expanded { Text(page.rawValue).font(.system(size:13)); Spacer() } }
                            .padding(.vertical,10).padding(.horizontal,8).frame(maxWidth:.infinity)
                            .background(model.page == page ? Color.blue.opacity(0.10) : .clear).cornerRadius(7)
                            .foregroundStyle(model.page == page ? Color.blue : Color.primary)
                    }.buttonStyle(.plain).help(page.rawValue).accessibilityIdentifier("phasec.nav.\(page.id)")
                }
                Spacer(minLength:8)
                if expanded {
                    Divider()
                    Text("可用空间：" + ((status.facts["disk_free"] as? Int64).map(formatBytes) ?? "—")).font(.system(size:10))
                    Text("中心数据库：" + (status.facts["database"] == nil ? "未连接" : "只读状态已连接")).font(.system(size:10))
                    Text("© 2026 Horizon-94").font(.system(size:9))
                    Text("版本 \(bundledAppVersion) · build\(Bundle.main.object(forInfoDictionaryKey:"CFBundleVersion") as? String ?? "—")").font(.system(size:10)).accessibilityIdentifier("phasec.bundle.identity")
                }
            }.frame(minHeight: max(0, geometry.size.height - (expanded ? 24 : 18)), alignment: .top)
            .padding(expanded ? 12 : 9)
            }.frame(width:expanded ? 168 : 54).frame(maxHeight:.infinity).background(Color(nsColor:.windowBackgroundColor))
            Divider()
            }
            Group {
                switch model.page {
                case .newTask: PhaseCNewTask()
                case .running: PhaseCRunning(status:status)
                case .history: PhaseCHistory()
                case .search: SearchPage()
                case .favorites: PhaseCFavorites()
                case .editorial: EditorialBoardPage()
                case .reference: EditorialReferencePage()
                case .groups: PhaseCGroups()
                case .settings: PhaseCSettings()
                }
            }.environment(\.responsiveWidth, geometry.size.width - (compact ? 0 : (expanded ? 169 : 55))).frame(minWidth:0,maxWidth:.infinity,maxHeight:.infinity).background(model.page == .search ? Color(nsColor:.windowBackgroundColor) : Color(nsColor:.underPageBackgroundColor))
        }
        }
        }.frame(minWidth:560,minHeight:398)
        .onAppear { status.refresh() }.onReceive(timer) { _ in if !model.terminating {status.refresh()} }
    }
}

private struct PhaseCNewTask: View {
    @EnvironmentObject var model: ArchiveModel
    @State private var confirmation = false
    @State private var preflight = ""
    private var full: Bool { model.taskMode == "第一次完整整理" }
    private var pathValid: Bool {
        let a=URL(fileURLWithPath:model.sourceFolder).standardizedFileURL.resolvingSymlinksInPath().path
        let b=URL(fileURLWithPath:model.libraryFolder).standardizedFileURL.resolvingSymlinksInPath().path
        return !model.sourceFolder.isEmpty && !model.libraryFolder.isEmpty && a != b && !a.hasPrefix(b+"/") && !b.hasPrefix(a+"/")
    }
    private var ready: Bool { full ? pathValid && !model.taskName.isEmpty : !model.selectedExistingTaskPath.isEmpty }
    var body: some View {
        ScrollView { VStack(alignment:.leading,spacing:18) {
            PageHeader(title:"新建任务",subtitle:"建立新的素材库，或对已有素材库执行增量更新。")
            ResponsiveRow(alignment:.top,spacing:16) {
                VStack(spacing:12) {
                    PhaseCBox(title:"1  选择素材来源") {
                        if full { HStack { TextField("请选择图片 / 视频所在文件夹",text:$model.sourceFolder); Button("浏览…") { model.chooseSourceFolder() } }
                            Text("原始素材只读 · 仅接收 IMAGE + VIDEO").font(.caption).foregroundStyle(.secondary)
                            PhaseCDisabled(title:"检测素材",reason:"创建任务时由正式 inventory 发现素材；不在 UI 中额外扫描素材库。")
                        } else { Text("沿用目标素材库已有来源") }
                    }
                    PhaseCBox(title:"2  整理模式") {
                        Picker("主要整理方式",selection:$model.taskMode) { Text("完整整理").tag("第一次完整整理"); Text("增量处理").tag("增量整理") }.pickerStyle(.segmented)
                        DisclosureGroup("维护与补处理 · 4 项") {
                            VStack(alignment:.leading,spacing:10) {
                                PhaseCDisabled(title:"修复缺失内容"); PhaseCDisabled(title:"重建搜索入口")
                                PhaseCDisabled(title:"补充音频搜索",reason:"视频 VAD / Whisper 随完整流程处理；旧补处理命令不恢复。")
                                PhaseCDisabled(title:"闲时人物识别 / 归并",reason:"未来接口，当前不可用")
                            }.padding(.top,8)
                        }
                        if !full { Picker("目标素材库",selection:$model.selectedExistingTaskPath) { Text("请选择").tag(""); ForEach(model.libraryCatalog) { l in Text(l.taskName).tag(l.taskPath) } }
                            Text("旧 1.2.8 / build149 任务不可直接 Resume / Activate").font(.caption).foregroundStyle(.secondary) }
                    }
                    PhaseCBox(title:"3  任务与索引位置") {
                        TextField("任务名称",text:$model.taskName)
                        if full { HStack { TextField("索引根目录",text:$model.libraryFolder); Button("浏览…") { model.chooseLibraryFolder() } } }
                        Text("素材目录和索引目录必须分离，不可相同或互相嵌套。").font(.caption).foregroundStyle(.secondary)
                    }
                    PhaseCBox(title:"4  处理方案") {
                        DisclosureGroup("使用当前稳定生产策略") { Text("图片 + 视频\nQwen 3.5 9B · 1 Worker\nWhisper · 1 Worker\nTail Boost = false\n视频语音随主流程处理").font(.callout).padding(.top,8) }
                    }
                    PhaseCBox(title:"5  创建前检查") {
                        Button("检查任务配置") { preflight = ready ? "配置完整；可以创建任务。" : "请填写必要路径、名称，或选择目标素材库。" }
                        if !preflight.isEmpty { Text(preflight).font(.caption) }
                    }
                }
                PhaseCBox(title:"任务摘要") {
                    summary("任务名称",model.taskName);summary("素材来源",full ? model.sourceFolder : "沿用目标素材库")
                    summary("索引根目录",full ? model.libraryFolder : model.selectedExistingTaskPath)
                    summary("整理模式",full ? "完整整理" : "增量处理");summary("处理范围","IMAGE + VIDEO")
                    Text("<项目名称> / task.json\nworkspace / runtime / video.sqlite").font(.caption.monospaced()).padding(10).background(Color.secondary.opacity(0.06)).cornerRadius(6)
                    Button("清空路径") { model.sourceFolder="";model.libraryFolder="" }
                    Button(full ? "创建并开始完整整理" : "开始增量处理") { confirmation=true }.buttonStyle(.borderedProminent).disabled(!ready || model.actionInProgress)
                    if !model.actionMessage.isEmpty { Text(model.actionMessage).font(.caption).foregroundStyle(model.actionFailed ? .red : .secondary) }
                }.frame(maxWidth:280)
            }
        }.padding(24) }.textFieldStyle(.roundedBorder)
        .alert("确认创建任务",isPresented:$confirmation) { Button("返回修改",role:.cancel) {}; Button("确认创建") { model.startTask() } } message: { Text("确认创建任务；原始素材保持只读。") }
    }
    private func summary(_ name:String,_ value:String)->some View { VStack(alignment:.leading,spacing:4) { Text(name).font(.caption).foregroundStyle(.secondary);Text(value.isEmpty ? "尚未选择" : value).font(.callout).textSelection(.enabled) } }
}

// Build151 running page only. Adapters observe; they never own processing.
private final class Build151RuntimeViewModel: ObservableObject {
    @Published var facts:[String:Any]=[:]
    @Published var error=""
    @Published var refreshed:Date?=nil
    private var busy=false
    func refresh() {
        guard !busy && !Build151ChildOwner.closing else {return};busy=true
        let root=Bundle.main.bundleURL.appendingPathComponent("Contents")
        DispatchQueue.global(qos:.utility).async {
            let p=Process(),pipe=Pipe(),errors=Pipe()
            p.executableURL=root.appendingPathComponent("Helpers/PipelinePython/system")
            p.arguments=[root.appendingPathComponent("Resources/build151_runtime_status.py").path,"--snapshot"]
            p.environment=phaseCEnvironment();p.standardOutput=pipe;p.standardError=errors
            let readBegan=Date();var protocolDiagnostic=""
            do {
                try p.run(); Build151ChildOwner.register(p)
                DispatchQueue.global().asyncAfter(deadline:.now()+12) {if p.isRunning {p.terminate()}}
                // Status adapters have bounded stderr; task controls use existing ArchiveModel API.
                let data=pipe.fileHandleForReading.readDataToEndOfFile()
                let detail=errors.fileHandleForReading.readDataToEndOfFile();p.waitUntilExit(); Build151ChildOwner.finish(p)
                protocolDiagnostic="exit=\(p.terminationStatus), bytes=\(data.count), seconds=\(Int(Date().timeIntervalSince(readBegan))), stderr=\(String(data:detail,encoding:.utf8)?.prefix(200) ?? "")"
                let result=(try JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]
                DispatchQueue.main.async {
                    if p.terminationStatus==0 && result["status"] as? String=="PASS" {
                        var next=result
                        if result["snapshot_partial"] as? Bool==true,
                           result["task_path"] as? String==self.facts["task_path"] as? String {
                            for (key,value) in self.facts where next[key]==nil {next[key]=value}
                            next["statistics_observed_at"]=self.facts["statistics_observed_at"] ?? self.facts["observed_at"]
                        }
                        self.facts=next;self.refreshed=Date();self.error=""
                    } else {self.error=result["error"] as? String ?? String(data:detail,encoding:.utf8) ?? "状态读取失败"}
                    self.busy=false
                }
            } catch {let diagnostic=protocolDiagnostic;DispatchQueue.main.async {self.error=error.localizedDescription+" · "+diagnostic;self.busy=false}}
        }
    }
}
private struct Build151Disclosure<Content:View>:View {
    let title:String
    @ViewBuilder var content:()->Content
    @State private var expanded=false
    @State private var hovered=false
    init(_ title:String,@ViewBuilder content:@escaping ()->Content){self.title=title;self.content=content}
    var body:some View {VStack(alignment:.leading,spacing:6){
        Button{expanded.toggle()} label:{HStack{Text(title);Spacer(minLength:8);Image(systemName:expanded ? "chevron.down":"chevron.right").foregroundStyle(.secondary)}.padding(.horizontal,8).frame(maxWidth:.infinity,minHeight:34,alignment:.leading).contentShape(Rectangle()).background(hovered ? Color.primary.opacity(0.05):.clear).cornerRadius(6)}.buttonStyle(.plain).onHover{hovered=$0;($0 ? NSCursor.pointingHand:NSCursor.arrow).set()}
        if expanded {content()}
    }}
}
private struct Build151StatusPage:View {
    @StateObject private var vm=Build151RuntimeViewModel()
    @EnvironmentObject var model:ArchiveModel
    @State private var confirmStop=false
    @State private var pendingAction:String?=nil
    @State private var controlError=""
    @State private var acceptedAt:Date?=nil
    @State private var requestedAt:Date?=nil
    @Environment(\.colorScheme) private var scheme
    @State private var selected="qwen"
    private let timer=Timer.publish(every:5,on:.main,in:.common).autoconnect()
    private var bg:Color { tone(0x0f1115,0xf3f5f7) }
    private var panel:Color { tone(0x1b2027,0xffffff) }
    private var inset:Color { tone(0x12161b,0xf1f4f8) }
    private var border:Color { tone(0x2a3038,0xdce1e8) }
    private var f:[String:Any] { vm.facts }
    private var stages:[[String:Any]] { f["scoped_stages"] as? [[String:Any]] ?? [] }
    private var active:[[String:Any]] { f["activeTasks"] as? [[String:Any]] ?? [] }
    private var sources:[String:Any] { f["sources"] as? [String:Any] ?? [:] }
    private var errors:[String:Any] { f["errors"] as? [String:Any] ?? [:] }
    private var total:Int { sources.values.compactMap{$0 as? Int}.reduce(0,+) }
    private var committed:Int { f["search_committed"] as? Int ?? 0 }
    private var raw:String { f["task_state"] as? String ?? "unknown" }
    private var lifecycle:String {f["lifecycle_state"] as? String ?? "UNKNOWN"}
    private var controls:[String:Any] {f["controls"] as? [String:Any] ?? [:]}
    private var indexActivity:[String:Any] {f["index_activity"] as? [String:Any] ?? [:]}
    private var runtimeActivityLabel:String {let activity=f["runtime_activity"] as? String ?? "UNKNOWN";if activity=="ACTIVE"{return "系统运行中"};return indexActivity["label"] as? String ?? (activity=="WAITING" ? "系统等待中":"状态过期/未知")}
    private var terminalSearch:[String:Any] {f["terminal_search"] as? [String:Any] ?? [:]}
    private var ended:Bool {terminalSearch["ended"] as? Bool==true && f["scheduler_active"] as? Bool==false}
    private var state:String {if ended {return (terminalSearch["unpublished_sources"] as? Int ?? 0)>0 ? "本次处理已结束，部分素材未完成":"本次处理已结束"};if runtimeStale{return "状态过期/未知"};return ["PREPARING":"正在准备处理","INITIALIZING":"正在初始化","RUNNING":runtimeActivityLabel,"STARTING":"正在启动","STOPPING":"正在停止","STOPPED_RESUMABLE":"已停止，可恢复","PAUSED":"已暂停","COMPLETED":"处理完成","FAILED_RESUMABLE":"处理失败 · 可继续","FAILED":"处理失败","BLOCKED":"处理受阻","STOPPED":"已停止","IDLE":"空闲"][lifecycle] ?? "正在读取状态"}
    private var stateKey:String {if runtimeStale{return "UNKNOWN"};if lifecycle=="RUNNING" {if f["runtime_activity"] as? String=="WAITING"{return "PENDING"};if f["runtime_activity"] as? String=="UNKNOWN"{return "UNKNOWN"}};return ["RUNNING","STARTING","STOPPING"].contains(lifecycle) ? "RUNNING":lifecycle=="COMPLETED" ? "DONE":["FAILED","FAILED_RESUMABLE","BLOCKED"].contains(lifecycle) ? "TERMINAL_FAILED":"IDLE"}
    private var isRunning:Bool {lifecycle=="RUNNING" && f["scheduler_active"] as? Bool==true}
    private var controlsBusy:Bool {pendingAction != nil || model.actionInProgress}
    private var folders:[[String:Any]] { f["folders"] as? [[String:Any]] ?? [] }
    private func tone(_ dark:Int,_ light:Int)->Color { let n=scheme == .dark ? dark:light;return Color(red:Double((n>>16)&255)/255,green:Double((n>>8)&255)/255,blue:Double(n&255)/255) }
    private func color(_ s:String)->Color { switch s {case "RUNNING":return tone(0x3b82f6,0x2563eb);case "DONE":return tone(0x22c55e,0x16803d);case "PENDING","RETRY_PENDING","WARNING":return tone(0xeab308,0x966b00);case "TERMINAL_FAILED","BLOCKED":return .red;default:return tone(0x94a3b8,0x64748b)} }
    private func str(_ value:Any?)->String { guard let v=value, !(v is NSNull) else{return "—"};if let n=v as? NSNumber{return n.stringValue};return v as? String ?? "—" }
    private func bytes(_ v:Any?)->String { guard let n=v as? NSNumber else{return "—"};return formatBytes(n.int64Value) }
    private func metric(_ name:String,_ value:String)->some View { VStack(alignment:.leading,spacing:5){Text(name).font(.system(size:11)).foregroundStyle(.secondary);Text(value).font(.system(size:15,weight:.semibold).monospacedDigit()).textSelection(.enabled)}.frame(maxWidth:.infinity,alignment:.leading) }
    private func badge(_ name:String,_ key:String)->some View { HStack(spacing:5){Circle().fill(color(key)).frame(width:6,height:6);Text(name).font(.system(size:11))}.foregroundStyle(color(key)) }
    private func box<C:View>(_ title:String,@ViewBuilder content:()->C)->some View { VStack(alignment:.leading,spacing:10){if !title.isEmpty {Text(title).font(.system(size:13,weight:.semibold))};content()}.padding(12).frame(maxWidth:.infinity,alignment:.leading).background(panel).cornerRadius(10).overlay(RoundedRectangle(cornerRadius:10).stroke(border,lineWidth:1)) }
    private func detail<C:View>(@ViewBuilder content:()->C)->some View { VStack(alignment:.leading,spacing:10){content()}.padding(12).frame(maxWidth:.infinity,alignment:.leading).background(inset).cornerRadius(8) }
    private func bar(_ done:Int,_ all:Int,_ key:String)->some View { GeometryReader { g in ZStack(alignment:.leading){Capsule().fill(border);Capsule().fill(color(key)).frame(width:g.size.width*(all>0 ? min(1,Double(done)/Double(all)):0))} }.frame(height:5) }
    private func label(_ k:String)->String { ["frame_basics":"基础画面处理","insightface":"人物检测","qwen":"Qwen","whisper":"Whisper","embedding":"文本向量","audio_extract":"FFmpeg · 音频抽取","vad":"VAD","extract_frames":"FFmpeg · 抽帧","yoloe":"YOLOE","openclip":"OpenCLIP","ocr":"OCR"][k] ?? k }
    private func rate(_ k:String,_ window:String="1",history:Bool=false)->String { if !history && (runtimeStale || f["snapshot_partial"] as? Bool==true){return "—"};guard history || isRunning else {return "—"};if !history && ["source","fts","vector","visual"].contains(k) {guard let end=f["throughput_history_end"] as? Double,Date().timeIntervalSince1970-end<30 else{return "—"}};let t=f["throughput"] as? [String:[String:Any]] ?? [:];guard let n=t[k]?[window] as? NSNumber else {return "—"};return String(format:"%.2f / 分",n.doubleValue) }
    private func stateText(_ k:String)->String { ["RUNNING":"正在运行","PENDING":"等待","RETRY_PENDING":"重试","TERMINAL_FAILED":"最终失败","BLOCKED":"阻塞","DONE":"完成","IDLE":"空闲","REGISTERED_DONE":"当前已登记任务完成","UNKNOWN":"状态未知","ACTIVE":"正在处理","WAITING":"等待","NO_CANDIDATE":"空闲无候选","LOADING":"正在加载","WAITING_COMMIT":"等待提交","RESOURCE_WAIT":"资源受限","STOPPED":"已停止"][k] ?? k }
    var body:some View {
        GeometryReader { geo in ScrollView(.vertical) { VStack(alignment:.leading,spacing:12) {
            ResponsiveRow(alignment:.top){VStack(alignment:.leading,spacing:4){Text("运行状态").font(.system(size:25,weight:.bold));Text("素材处理、模型工作、搜索闭环与机器资源").foregroundStyle(.secondary)};Spacer();ForEach(["RUNNING","DONE","PENDING","TERMINAL_FAILED","IDLE"],id:\.self){badge(stateText($0),$0)}}
            if vm.facts.isEmpty {Text("正在读取真实状态 · 暂无数据")} else {
            overview
            if model.actionMessage.hasPrefix("现有 ") || model.actionMessage.hasPrefix("正在核验并追加") || model.actionFailed {
                Text(model.actionMessage).font(.system(size:12)).foregroundStyle(model.actionFailed ? .red : .secondary).textSelection(.enabled).accessibilityIdentifier("build151.project.expansion-summary")
            }
            throughput
            ResponsiveRow(alignment:.top,spacing:12){box("素材处理流水线") { pipeline("图片","image",false);Divider();pipeline("视频 · 画面","video",false);Divider();pipeline("视频 · 声音","video",true) }.frame(maxWidth:.infinity);workers.frame(maxWidth:350)}
            ResponsiveRow(alignment:.top,spacing:12){searchPanel;machine;database}
            errorPanel
            }
            HStack {Text(vm.refreshed.map{"最近更新 "+$0.formatted(date:.omitted,time:.standard)} ?? "正在连接只读状态…");Spacer();Text("每 5 秒自动刷新真实状态");Button("手动刷新"){vm.refresh()}}.font(.system(size:10)).foregroundStyle(.secondary)
            if f["snapshot_partial"] as? Bool==true {Text("部分统计暂不可用，保留上次统计（\(Date(timeIntervalSince1970: (f["statistics_observed_at"] as? Double) ?? (f["observed_at"] as? Double) ?? 0).formatted(date: .omitted,time: .standard))）；运行与停止状态已更新。").foregroundStyle(.orange)}
            if !vm.error.isEmpty {Text("读取失败 · 保留上次数据："+vm.error).foregroundStyle(.red)}
        }.padding(20).frame(width:geo.size.width,alignment:.topLeading) } }.background(bg).foregroundStyle(tone(0xe8edf3,0x18212d)).onAppear{vm.refresh()}.onReceive(timer){_ in if !model.terminating {vm.refresh()}}
        .onChange(of:model.actionInProgress){busy in
            guard pendingAction != nil,!busy else {return}
            if model.actionFailed {controlError=(pendingAction=="resume" ? "继续失败：":"停止失败：")+model.actionMessage;pendingAction=nil;acceptedAt=nil}
            else {acceptedAt=Date();vm.refresh()}
        }
        .onChange(of:vm.refreshed){_ in checkControlResult()}
        .alert("停止当前处理？",isPresented:$confirmStop){
            Button("返回",role:.cancel){}
            Button("停止",role:.destructive){runControl("stop")}
        } message:{Text("已完成结果会保留，可恢复能力以当前任务状态为准。")}

    }
    private var overview:some View { box("") {
        ResponsiveRow(alignment:.top,spacing:18){VStack(alignment:.leading,spacing:6){badge(state,stateKey);if isRunning, let detail=indexActivity["detail"] as? String {Text(detail).font(.system(size:12)).foregroundStyle(.secondary)};taskControls;Text(f["library_name"] as? String ?? "尚未选择素材库").font(.system(size:12)).foregroundStyle(.secondary)}.frame(maxWidth:.infinity,alignment:.leading);metric((runtimeStale || f["snapshot_partial"] as? Bool==true) ? "可搜索素材（上次统计）":"可搜索素材",f["sources"]==nil ? "—":"\(str(f["search_committed"])) / \(total)");metric(ended ? "历史运行耗时":"本次运行时间",duration(f["elapsed_seconds"]));if !ended {metric("当前瓶颈 · 实测依据",bottleneck);metric(isRunning ? "预计剩余时间":"恢复后预计剩余",eta)}}
        ResponsiveRow(spacing:24){VStack(alignment:.leading,spacing:8){HStack{Text("搜索闭环进度");Spacer();Text(total>0 ? String(format:"%.1f%%",100*Double(committed)/Double(total)):"—")};bar(committed,total,"DONE")}.font(.system(size:11));HStack{metric("图片",str(sources["image"]));metric("视频",str(sources["video"]));metric("独立音频",sources.isEmpty ? "—":str(sources["audio"] ?? 0));metric("总素材",sources.isEmpty ? "—":String(total))}.frame(maxWidth:440)}
        backlogDetail
    } }
    private var taskControls:some View {VStack(alignment:.leading,spacing:6){
        if ended {
            Text("已发布 \(str(terminalSearch["published_sources"])) 个来源 · 未发布 \(str(terminalSearch["unpublished_sources"])) 个来源").font(.caption)
            Text("无合法待办；失败与重试历史完整保留。").font(.caption).foregroundStyle(.secondary)
            Button("搜索素材") {model.page = .search}.buttonStyle(.borderedProminent)
            Button("查看未完成素材/失败原因") {if let library=model.snapshot?.existingLibraries.first(where:{$0.isActive}) {model.loadHistoryDetail(library)};model.page = .history}
        }
        HStack(spacing:8){
            if let projectId=f["task_id"] as? String,!projectId.isEmpty {
                Button("Add Folder / 追加文件夹"){model.addProjectFolder(projectId)}.disabled(controlsBusy || f["scheduler_active"] as? Bool==true).accessibilityIdentifier("build151.project.add-folder")
            }
            if !ended && (controls["resume"] as? Bool==true || pendingAction=="resume") {
                Button(pendingAction=="resume" ? "正在继续…":"继续处理"){runControl("resume")}.buttonStyle(.borderedProminent).disabled(controlsBusy || !vm.error.isEmpty).accessibilityIdentifier("build151.control.resume")
            }
            if controls["stop"] as? Bool==true || pendingAction=="stop" {
                Button(pendingAction=="stop" ? "正在停止…":"停止",role:.destructive){confirmStop=true}.disabled(controlsBusy).accessibilityIdentifier("build151.control.stop")
            }
            if lifecycle=="COMPLETED" {Text("已完成").foregroundStyle(color("DONE"))}
        }
        if pendingAction != nil {Text(acceptedAt==nil ? "正在提交操作…":"操作已提交，等待后台真实状态").font(.system(size:10)).foregroundStyle(.secondary)}
        if !controlError.isEmpty {Text(controlError).font(.system(size:11)).foregroundStyle(.red).textSelection(.enabled)}
    }}
    private func runControl(_ action:String){
        guard !controlsBusy else {return}
        controlError="";pendingAction=action;acceptedAt=nil;requestedAt=Date()
        if action=="resume" {model.resumeTask()} else {model.stopTask()}
    }
    private func checkControlResult(){
        guard let action=pendingAction,let accepted=acceptedAt,let updated=vm.refreshed,updated>=accepted else {return}
        if action=="resume" && build151ResumeAccepted(lifecycle:lifecycle,schedulerActive:f["scheduler_active"] as? Bool==true) {pendingAction=nil;acceptedAt=nil;controlError=""}
        else if action=="stop" && f["scheduler_active"] as? Bool==false && ["STOPPED_RESUMABLE","STOPPED","PAUSED","FAILED","FAILED_RESUMABLE","COMPLETED"].contains(lifecycle) {pendingAction=nil;acceptedAt=nil;controlError=""}
        else if action=="resume" && build151ResumeFailed(lifecycle:lifecycle,schedulerActive:f["scheduler_active"] as? Bool==true) {controlError="继续失败："+(f["lifecycle_error"] as? String ?? "后台未能进入运行状态");pendingAction=nil;acceptedAt=nil}
        else if let requested=requestedAt,Date().timeIntervalSince(requested)>120 {controlError="后台仍未确认状态切换，请查看真实状态和任务日志；不会强杀 worker。"}
    }
    private var backlogs:[[String:Any]] {f["backlogs"] as? [[String:Any]] ?? []}
    private var etaFact:[String:Any] {f["display_eta"] as? [String:Any] ?? [:]}
    private func etaText(_ value:Any?)->String {guard let n=value as? NSNumber else{return "暂无法估算"};if n.doubleValue<=0 {return "暂无剩余估计"};if n.doubleValue<60 {return "约 \(Int(ceil(n.doubleValue))) 秒"};return "约 \(Int(ceil(n.doubleValue/60))) 分钟"}
    private var eta:String {if runtimeStale || f["snapshot_partial"] as? Bool==true{return "暂无法估算"};return etaText(etaFact["seconds"])}
    private var bottleneck:String {guard let row=f["primary_bottleneck"] as? [String:Any],row["confidence"] as? String=="MEDIUM" else{return isRunning && indexActivity["fresh"] as? Bool==true && f["runtime_activity"] as? String=="INDEXING" ? "索引覆盖 / 提交中":"正在判断"};return label(row["stage"] as? String ?? "")+" · "+str(row["remaining"])+" 项"}
    private var backlogDetail:some View {VStack(alignment:.leading,spacing:7){
        let primary=(f["primary_bottleneck"] as? [String:Any])?["stage"] as? String
        Text("其它等待："+backlogs.filter{$0["stage"] as? String != primary}.map{label($0["stage"] as? String ?? "")+" "+str($0["remaining"])}.joined(separator:" · ")).font(.system(size:11)).foregroundStyle(.secondary)
        if etaFact["status"] as? String=="READY" {Text((etaFact["basis"] as? String ?? "")+" · 已知工作近似值，不含尚未生成的工作。停止状态表示恢复后的估计耗时。").font(.system(size:10)).foregroundStyle(.secondary)}
        Build151Disclosure("全部积压") {detail{
            if backlogs.isEmpty {
                Text(runtimeStale ? "模型待办状态过期/未知；不能据此判定整库完成。" : isRunning ? "模型队列当前没有待办；"+(indexActivity["label"] as? String ?? "索引状态未知")+"。索引覆盖、来源闭合与发布的剩余量暂未知，不代表整库完成。" : "当前没有模型待办；来源闭合与发布请以上方搜索进度及保留失败为准。")
                    .font(.system(size:11)).foregroundStyle(.secondary)
                if let count=(f["errors"] as? [String:Any])?["finalFailed"] as? Int,count>0 {
                    Text("保留最终失败 \(count) 项任务（不等同于来源数）；不会自动再次重试。")
                        .font(.system(size:11)).foregroundStyle(color("TERMINAL_FAILED"))
                }
            }
            LazyVGrid(columns:[GridItem(.flexible()),GridItem(.fixed(90)),GridItem(.fixed(110)),GridItem(.fixed(120)),GridItem(.fixed(90))],alignment:.leading,spacing:9){
                ForEach(["阶段","剩余数量","当前速度","预计阶段耗时","状态"],id:\.self){Text($0).foregroundStyle(.secondary)}
                ForEach(Array(backlogs.enumerated()),id:\.offset){_,r in Group{Text(label(r["stage"] as? String ?? ""));Text(str(r["remaining"]));Text(rate(r["stage"] as? String ?? ""));Text(etaText(r["eta_seconds"]));Text(stateText(r["state"] as? String ?? "IDLE")).foregroundStyle(color(r["state"] as? String ?? "IDLE"))}}
            }.font(.system(size:11))
            ForEach(Array(backlogs.enumerated()),id:\.offset){_,r in Text(label(r["stage"] as? String ?? "")+" ETA 依据："+(r["rate_basis"] as? String ?? "暂无可用吞吐依据")).font(.system(size:10)).foregroundStyle(.secondary)}
        }}
    }}
    private func duration(_ v:Any?)->String { guard let n=v as? NSNumber else{return "—"};let s=max(0,n.intValue);return String(format:"%02d:%02d:%02d",s/3600,s/60%60,s%60) }
    private let speedKeys=["source","qwen","whisper","embedding","fts","vector","visual"]
    private let speedNames=["整体素材","Qwen","Whisper","文本向量","全文索引 / FTS","文本向量库","视觉向量"]
    private var throughput:some View {box("处理速度") {
        if isRunning {HStack{ForEach(Array(speedKeys.enumerated().filter{rate($0.element) != "—"}),id:\.offset){i,k in metric(speedNames[i],rate(k))}}} else {Text("处理已结束；历史速度可在下方查看").font(.caption).foregroundStyle(.secondary)}
        Build151Disclosure(isRunning ? "展开滚动平均 · 最近 1 / 5 / 15 分钟":"展开历史窗口 · 最近 1 / 5 / 15 分钟平均") {detail {ResponsiveRow(alignment:.top){ForEach(Array(speedKeys.enumerated()),id:\.offset){i,k in VStack(alignment:.leading,spacing:6){Text(speedNames[i]);Text(trend(k)).foregroundStyle(.secondary);ForEach(["1","5","15"],id:\.self){w in Text("最近 "+w+" 分钟平均\n"+rate(k,w,history:true))}}.frame(maxWidth:.infinity,alignment:.leading)}}.font(.system(size:10));Text("不同滚动窗口分别计算，数值无需单调；缺少连续采样时显示 —。停止时为历史窗口，当前速度保持 —。").font(.caption).foregroundStyle(.secondary);if let end=f["throughput_history_end"] as? Double {Text("素材 / 索引计数窗口截至 "+Date(timeIntervalSince1970:end).formatted(date:.omitted,time:.standard)+"；模型速度按实际成功任务的时钟窗口计算。").font(.caption).foregroundStyle(.secondary)}}}
    }}
    private func trend(_ key:String)->String {let t=(f["throughput"] as? [String:[String:Any]])?[key] ?? [:];guard let a=t["1"] as? Double,let b=t["5"] as? Double,b>0 else{return "暂无趋势"};return a>b*1.15 ? "↑ 最近加快":a<b*0.85 ? "↓ 最近变慢":"≈ 基本稳定"}
    private func scopedRate(_ kind:String,_ stage:String)->String {guard isRunning && !runtimeStale && f["snapshot_partial"] as? Bool != true else {return "—"};let t=f["stage_throughput"] as? [String:Double] ?? [:];guard let n=t[kind+":"+stage] else{return "—"};return String(format:"%.2f/分",n)}
    private func rows(_ kind:String,_ audio:Bool)->[[String:Any]] {let current=kind=="video" ? (f["video_live_stages"] as? [[String:Any]] ?? stages):stages;return current.filter{ $0["kind"] as? String==kind && (audio ? ["audio_extract","vad","whisper"].contains($0["stage"] as? String ?? "") : !["audio_extract","vad","whisper"].contains($0["stage"] as? String ?? ""))}}
    private func count(_ rows:[[String:Any]],_ status:String)->Int { rows.filter{$0["status"] as? String==status}.reduce(0){$0+($1["count"] as? Int ?? 0)} }
    private func stageState(_ rows:[[String:Any]])->String {if rows.isEmpty && f["snapshot_partial"] as? Bool==true{return "UNKNOWN"};for k in ["RUNNING","BLOCKED","RETRY_PENDING","PENDING","TERMINAL_FAILED"] {if count(rows,k)>0{return k}};return rows.isEmpty ? "IDLE":"REGISTERED_DONE"}
    private func pipeline(_ title:String,_ kind:String,_ audio:Bool)->some View {
        let rr=rows(kind,audio),state=stageState(rr),names=Set(rr.compactMap{$0["stage"] as? String}).sorted()
        let primary=audio ? "whisper":"qwen",tasks=active.filter{$0["stage"] as? String==primary && $0["kind"] as? String==kind}
        let scope=(f["overview"] as? [String:[String:Any]])?[kind] ?? [:]
        return VStack(alignment:.leading,spacing:10){
            HStack {Text(title).bold();Text(audio ? "总范围 \(str(sources[kind])) 个视频":"\(str(scope["complete"])) / \(str(sources[kind])) 个来源 · backend terminal").monospacedDigit();Spacer();badge("\(count(rr,"RUNNING")) 运行",count(rr,"RUNNING")>0 ? "RUNNING":"IDLE");badge("\(count(rr,"PENDING")) 等待","PENDING")}.font(.system(size:12))
            HStack{Text("主要 Worker（共享） · "+label(primary));Spacer();Text(scopedRate(kind,primary));badge(stateText(truth(primary)["lane_state"] as? String ?? "UNKNOWN"),truth(primary)["inference_state"] as? String=="ACTIVE" ? "RUNNING":"IDLE")}.font(.system(size:11)).foregroundStyle(.secondary)
            if audio {audioTruth;Text("剩余片段 \(count(rr.filter{$0["stage"] as? String=="whisper"},"PENDING")+count(rr.filter{$0["stage"] as? String=="whisper"},"RUNNING")) · Whisper \(activityText(primary))").font(.system(size:11))}
            if !audio {bar(scope["complete"] as? Int ?? 0,sources[kind] as? Int ?? 0,state)}
            if !audio && kind=="video" {visualTruth;Text("Qwen \(activityText(primary)) · 等待候选 \(count(rr.filter{$0["stage"] as? String=="qwen"},"PENDING"))").font(.system(size:11))}
            ForEach(Array(tasks.enumerated()),id:\.offset){_,t in Text(taskSummary(t)+" · "+sourceOrder(t)+" · 阶段累计完成 "+globalPosition(primary)).font(.system(size:11)).textSelection(.enabled)}
            Build151Disclosure("展开真实 Pipeline") {detail {
                LazyVGrid(columns:[GridItem(.flexible(minimum:105),alignment:.leading),GridItem(.fixed(94),alignment:.leading)]+Array(repeating:GridItem(.flexible(minimum:35),alignment:.leading),count:4)+[GridItem(.fixed(74),alignment:.leading),GridItem(.fixed(72),alignment:.leading)],alignment:.leading,spacing:11){
                    ForEach(["阶段","完成 / 已登记","运行","等待","重试","最终失败","当前速度","状态"],id:\.self){Text($0).foregroundStyle(.secondary)}
                    ForEach(names,id:\.self){name in let r=rr.filter{$0["stage"] as? String==name};Group {Text(label(name)+" · "+(["frame_basics","yoloe","openclip","insightface","ocr"].contains(name) ? "帧任务":name=="whisper" ? "片段":"任务"));Text("\(count(r,"DONE")) / \(r.reduce(0){$0+($1["count"] as? Int ?? 0)})");Text(String(count(r,"RUNNING")));Text(String(count(r,"PENDING")));Text(String(count(r,"RETRY_PENDING")));Text(String(count(r,"TERMINAL_FAILED")));Text(scopedRate(kind,name));Text(stateText(stageState(r))).foregroundStyle(color(stageState(r)))}}
                }.font(.system(size:10).monospacedDigit())
                Text(audio ? "片段阶段任务数；音频文本向量与画面共用 embedding，列于画面区域。":"完成 / 总量是各阶段实际任务数；一级素材完成数使用 backend source_progress。视频抽帧动态流入下游。").font(.system(size:10)).foregroundStyle(.secondary)
            }}
        }
    }
    private var visualTruth:some View {VStack(alignment:.leading,spacing:5){
        let v=f["visual_progress"] as? [String:Any] ?? [:]
        Text("视频抽帧 Source：已启动 "+str(v["started"])+" · 已抽完 "+str(v["completed"])+" / "+str(v["total"])+" 个视频 · 未启动 "+str(v["waiting"]))
        ForEach(Array((v["current"] as? [[String:Any]] ?? []).enumerated()),id:\.offset){_,r in Text("正在抽帧 · "+str(r["filename"])+" · FFmpeg · ACTIVE")}
        ForEach(Array((v["admitted_not_started"] as? [[String:Any]] ?? []).enumerated()),id:\.offset){_,r in Text("等待启动 FFmpeg · "+str(r["filename"])+(r["offline"] as? Bool==true ? " · 原片离线":""))}
        let frame=(f["frame_progress"] as? [String:Any])?["stages"] as? [String:[String:Int]] ?? [:]
        if let r=frame["frame_basics"] {Text("基础画面帧任务：\(r["DONE"] ?? 0) / \(r.values.reduce(0,+)) 帧任务 · 与视频 Source 分开")}
    }.font(.system(size:11))}
    private var audioTruth:some View {VStack(alignment:.leading,spacing:5){
        let a=f["audio_registered"] as? [String:[String:Any]] ?? [:]
        ForEach(["audio_extract","vad","whisper"],id:\.self){k in let r=a[k] ?? [:]
            Text(label(k)+"："+str(r["done"])+" / "+str(r["registered"])+(k=="whisper" ? " 已登记片段":" 已登记任务")+" · 等待 "+str(r["waiting"])+" · 运行 "+str(r["running"])+" · 重试 "+str(r["retrying"])+" · 最终失败 "+str(r["failed_final"]))
        }
        Text("已处理 / 已登记；总视频范围另列。当前已登记任务完成不代表全部视频声音闭合。")
    }.font(.system(size:11))}
    private func globalPosition(_ stage:String)->String {let row=truth(stage);return str(row["completed_count"])+" / "+str(row["registered_count"])}
    private func activityText(_ stage:String)->String {let row=truth(stage);return row["inference_state"] as? String=="ACTIVE" ? "正在推理":stateText(row["lane_state"] as? String ?? "UNKNOWN")}
    private var workerKeys:[String] {let base=["qwen","whisper","yoloe","openclip","insightface","ocr","embedding","vad","audio_extract"];return base+Set(active.compactMap{$0["stage"] as? String}).subtracting(base).sorted()}
    private var runtimeStale:Bool { !vm.error.isEmpty || (vm.refreshed.map{Date().timeIntervalSince($0)>12} ?? true) }
    private func truth(_ key:String)->[String:Any] {
        var row=(f["model_states"] as? [[String:Any]] ?? []).first{$0["stage"] as? String==key} ?? [:]
        if runtimeStale {row["inference_state"]="UNKNOWN";row["lane_state"]="UNKNOWN";row["model_residency"]="UNKNOWN";row["wait_reason"]="状态过期/未知；以下数量为上次观测"}
        return row
    }
    private func modelState(_ key:String,_ tasks:[[String:Any]])->String {
        let row=truth(key)
        return "任务 "+str(row["lane_state"])+" · Worker "+str(row["worker_process_state"])+" · Model "+str(row["model_residency"])+" · Inference "+str(row["inference_state"]) + ((row["wait_reason"] as? String ?? "").isEmpty ? "" : " · "+str(row["wait_reason"]))
    }
    private func sourceOrder(_ t:[String:Any])->String {
        guard t["kind"] as? String=="video" else{return "图片 · 无视频处理号"}
        let audio=t["source_processing_lane"] as? String=="audio"
        let name=audio ? "音频视频号":"画面视频号"
        guard let index=t["source_index"] as? Int,let total=t["source_total"] as? Int else{return name+"待抽取完成"}
        guard index > 0,total > 0,index <= total else{return name+"无效"}
        let audioDone=(((f["audio_registered"] as? [String:[String:Any]])?["audio_extract"])?["done"] as? Int)
        let visualDone=((f["visual_progress"] as? [String:Any])?["completed"] as? Int)
        guard let displayedDone=(audio ? audioDone:visualDone),index <= displayedDone else{return name+"等待抽取计数同步"}
        return name+" \(index) · "+(audio ? "已抽取 ":"已抽帧 ")+"\(min(total,displayedDone))"
    }
    private func taskSummary(_ t:[String:Any])->String { "\(str(t["source_name"])) · \(t["stage"] as? String=="whisper" ? "片段":"候选") \(str(t["position"])) / \(str(t["position_total"]))" }
    private func compactModelState(_ key:String)->String {
        let row=truth(key)
        if runtimeStale {return "状态更新中 / 暂不可用"}
        if !isRunning {return f["task_state"] as? String == "failed" || f["task_state"] as? String == "success" ? "已结束":"已停止"}
        if row["inference_state"] as? String == "ACTIVE" {
            return key == "whisper" ? "正在转写":key == "audio_extract" ? "正在抽取音频":"正在分析"
        }
        let lane=row["lane_state"] as? String ?? "UNKNOWN"
        if ["COMMITTING","WAITING_COMMIT","RESULT_READY"].contains(lane) {return "提交中"}
        if ["FAILED","FAILED_FINAL","TERMINAL_FAILED"].contains(lane) {return "异常"}
        let reason=row["wait_reason"] as? String ?? ""
        if reason.contains("资源") || reason.contains("内存") || reason.contains("resource") {return "等待资源"}
        if ["PENDING","BLOCKED","RETRY_PENDING","WAITING_UPSTREAM"].contains(lane) {return "等待上游"}
        if ["NO_CANDIDATE","IDLE","STOPPED"].contains(lane) {return "空闲"}
        return "状态暂不可用"
    }
    private func progressUnit(_ key:String)->String {
        if key == "qwen" {return "个候选"}; if key == "whisper" {return "个片段"}
        if key == "embedding" {return "份文档"}; if ["vad","audio_extract"].contains(key) {return "个音频任务"}
        return "个画面"
    }
    private var workers:some View {box("当前素材处理详情"){
        ForEach(workerKeys,id:\.self){k in
            let row=truth(k)
            let actual=isRunning && !runtimeStale && row["inference_state"] as? String == "ACTIVE"
            let tasks=actual ? active.filter{$0["stage"] as? String==k}:[]
            VStack(alignment:.leading,spacing:6){
                HStack{Text(k=="qwen" ? "Qwen 3.5 9B":label(k)).bold();Spacer();badge(compactModelState(k),actual ? "RUNNING":compactModelState(k)=="异常" ? "TERMINAL_FAILED":compactModelState(k).hasPrefix("等待") ? "PENDING":"IDLE")}
                Text("项目："+(f["library_name"] as? String ?? "未选择项目")).foregroundStyle(.secondary)
                ForEach(Array(tasks.enumerated()),id:\.offset){_,t in
                    Text("素材："+(t["source_name"] as? String ?? "名称暂不可用")).lineLimit(2).textSelection(.enabled)
                    Text(sourceOrder(t)).foregroundStyle(.secondary)
                    if let n=t["position"] as? Int,let total=t["position_total"] as? Int {Text("当前 \(n) / \(total) "+progressUnit(k))}
                }
                if actual && tasks.isEmpty {Text("正在处理；当前文件信息尚未同步").font(.caption).foregroundStyle(.secondary)}
                if !runtimeStale,let done=row["completed_count"] as? Int,let total=row["registered_count"] as? Int {Text("本阶段已完成 \(done) / 已登记 \(total) "+progressUnit(k)).font(.caption).foregroundStyle(.secondary)}
                if runtimeStale,let at=vm.refreshed {Text("上次更新："+at.formatted(date:.omitted,time:.standard)).font(.caption).foregroundStyle(.secondary)}
                Build151Disclosure("诊断详情") {Text(modelState(k,tasks)).font(.caption).textSelection(.enabled);Button("查看完整诊断"){selected=k}}
            }.font(.system(size:11)).padding(10).frame(maxWidth:.infinity,alignment:.leading).background(inset).cornerRadius(8)
        }
        Build151Disclosure("完整工程诊断"){inspector}
    }}
    private var inspector:some View {detail {Text(label(selected)+" · Worker Inspector").bold();let tasks=active.filter{$0["stage"] as? String==selected}
        Text(modelState(selected,tasks)).textSelection(.enabled)
        kv("observed_at",str(truth(selected)["observed_at"]));kv("fresh",str(truth(selected)["fresh"]))
        kv("近60秒完成",str(truth(selected)["completed_last_60s"])+" / 分钟");kv("实际 Worker PID",str(truth(selected)["worker_pids"]))
        if tasks.isEmpty {Text(truth(selected)["inference_state"] as? String=="ACTIVE" ? "正在处理上游产物；尚未形成下游任务登记。":"当前无任务 · 任务运行时展示 Source、文件、候选 / 片段与工程详情。").foregroundStyle(.secondary)}
        ForEach(Array(tasks.enumerated()),id:\.offset){_,t in
            kv("当前任务",label(selected));kv("Source",str(t["source"]));kv("文件",str(t["source_name"]));kv("当前链视频号",sourceOrder(t));kv("Candidate / Segment",str(t["position"])+" / "+str(t["position_total"]));kv("全局候选",globalPosition(selected));kv("Worker 数量",str(t["worker_count"]));kv("Retry",str(t["retry"]));kv("速度",rate(selected));kv("运行时间",duration(t["elapsed_seconds"]));
            Build151Disclosure("展开工程详情"){ForEach(["job_id","source_id","candidate_id","segment_id","lease_until","position_basis","source_order_basis"],id:\.self){key in if let v=t[key],!(v is NSNull) {kv(key,str(v))}}}
        }
    }.font(.system(size:11)) }
    private func kv(_ key:String,_ value:String)->some View {ResponsiveRow(alignment:.top){Text(key).foregroundStyle(.secondary).frame(width:100,alignment:.leading);Text(value).textSelection(.enabled);Spacer(minLength:0)}}
    private func binaryBytes(_ value:Any?)->String {guard let n=value as? NSNumber else{return "—"};let unit=n.doubleValue>=1_073_741_824 ? "GiB":"MiB";let divisor=n.doubleValue>=1_073_741_824 ? 1_073_741_824.0:1_048_576.0;return String(format:"%.2f %@",n.doubleValue/divisor,unit)}
    private var searchPanel:some View {box("搜索与索引"){
        metric("可搜索素材 / 总素材",sources.isEmpty ? "—":"\(str(f["search_committed"])) / \(total)");bar(committed,total,"DONE")
        let storage=f["search_storage"] as? [String:Any] ?? [:]
        Text("已识别共享存储 "+binaryBytes(storage["total_identifiable_bytes"])+" · 含 processing 数据，非索引独占大小").font(.system(size:10)).foregroundStyle(.secondary)
        HStack {metric("文件夹",String(folders.count));metric("完整闭环",String(folders.filter{$0["state"] as? String=="FOLDER_COMPLETE"}.count));metric("部分完成",String(folders.filter{$0["state"] as? String=="FOLDER_SEARCHABLE_PARTIAL"}.count));metric("等待提交",String(folders.filter{$0["state"] as? String=="FOLDER_PENDING"}.count))}
        Build151Disclosure("展开 Search / Index"){detail{let coverage=f["coverage"] as? [String:Any] ?? [:];ForEach(["Documents","FTS","Fusion","Vector","Visual Vector","Projection","closure pending","SEARCH_COMMITTED"],id:\.self){key in kv(key,coverage[key]==nil ? "暂无数据":str(coverage[key]))}
            ForEach(Array((storage["items"] as? [[String:Any]] ?? []).enumerated()),id:\.offset){_,r in kv(r["label"] as? String ?? "索引",r["size_text"] as? String ?? "暂无数据")}
            Text(storage["total_scope"] as? String ?? "暂无存储数据").foregroundStyle(.secondary)
        }.font(.system(size:11))}
    }}
    private func gib(_ value:Any?,digits:Int=2)->String {guard let n=value as? NSNumber else{return "—"};return String(format:"%.*f",digits,n.doubleValue/1_073_741_824)}
    private var machine:some View {box("机器状态") {let s=f["system"] as? [String:Any] ?? [:];let pressure=s["pressure_level"] as? Int ?? 0
        kv("系统 CPU · 1秒窗口",(s["cpu_total_percent"] as? Double).map{String(format:"%.1f%%",$0)} ?? "—")
        HStack{Text("内存压力").foregroundStyle(.secondary).frame(width:100,alignment:.leading);badge([1:"正常",2:"偏高",4:"严重"][pressure] ?? "暂无数据",[1:"DONE",2:"WARNING",4:"TERMINAL_FAILED"][pressure] ?? "IDLE")}
        kv("系统已占用（估算）",gib(s["system_occupied_estimate_bytes"])+" / "+gib(s["physical_memory_bytes"],digits:1)+" GiB")
        kv("压缩内存",gib(s["compressed_memory_bytes"])+" GiB");kv("Swap",gib(s["swap_used_bytes"])+" GiB");kv("磁盘可用",bytes(f["disk_free"]))
        Build151Disclosure("展开机器详情") {detail{
            kv("物理统一内存总量",gib(s["physical_memory_bytes"],digits:1)+" GiB")
            Text("系统已占用为估算，非 App 内存，也不冒充活动监视器的精确 Memory Used。")
            Text(s["memory_used_semantics"] as? String ?? "暂无公式")
            Text("来源："+(s["memory_metric_source"] as? String ?? "—"))
            Text(s["cpu_metric_semantics"] as? String ?? "—");Text(s["cpu_metric_source"] as? String ?? "—")
            Text("磁盘：索引 workspace 所在卷；系统内存单位 GiB。进程 RSS 可含共享页，不等于系统占用，不可直接相加。")
            let processes=s["processes"] as? [[String:Any]] ?? []
            ForEach(Array(processes.enumerated()),id:\.offset){_,p in kv((p["type"] as? String ?? "Worker")+" RSS",(p["rss_kib"] as? Double).map{String(format:"%.2f GiB",$0/1_048_576)} ?? "—");Text("PID "+str(p["pid"])+" · 进程 CPU（单核 100%） "+str(p["cpu_percent"])+"%")}
        }.foregroundStyle(.secondary)}
    }.font(.system(size:11))}
    private var database:some View {box("数据库状态") {let db=f["database"] as? [String:Any] ?? [:];let w=db["writer"] as? [String:Any] ?? [:];let fresh=(db["telemetry_at"] as? Double).map{Date().timeIntervalSince1970-$0<15} ?? false
        badge("数据库写入 · "+(fresh ? ((w["busy_locked"] as? Int ?? 0)>0 ? "偏慢":"正常"):"暂无实时 telemetry"),fresh ? "DONE":"IDLE")
        kv("SQLite 主文件",binaryBytes(db["size"]));kv("写入日志（WAL）",binaryBytes(db["wal_size"]));Text("SQLite 主文件与 WAL 分开；两者均不等于 Task Root 总占用。路径："+(db["path"] as? String ?? "—")).font(.system(size:10)).foregroundStyle(.secondary)
        Build151Disclosure("展开 Database Detail"){detail {Text("WAL 是 SQLite 写入期间的事务日志；处理量大时可能增长，checkpoint 后通常可回收。页面不会主动 checkpoint、truncate 或 VACUUM。").foregroundStyle(.secondary);ForEach(["P50_ms","P95_ms","max_hold_ms","busy_locked"],id:\.self){key in kv(key,str(w[key]))};Text("P50 / P95 / MAX 单位 ms；busy / locked 为 backend 合并计数。以上为最近真实采样，当前未更新时不能据此判定健康。").foregroundStyle(.secondary);if let t=db["telemetry_at"] as? Double {Text("采样于 "+Date(timeIntervalSince1970:t).formatted())}}}
    }.font(.system(size:11))}
    private var errorPanel:some View {box("状态与异常") {
        let failed=errors["finalFailed"] as? Int
        HStack{badge(failed==nil ? "最终失败：暂无数据":failed==0 ? "没有最终失败任务":"最终失败 \(failed!) 项",failed==nil ? "IDLE":failed==0 ? "DONE":"TERMINAL_FAILED");Spacer();badge("等待 "+str(errors["waiting"]),"PENDING");badge("Retry "+str(errors["retrying"]),(errors["retrying"] as? Int ?? 0)>0 ? "RETRY_PENDING":"IDLE");badge("Blocked "+str(errors["blocked"]),(errors["blocked"] as? Int ?? 0)>0 ? "BLOCKED":"IDLE")}
        let current=f["current_job_state_totals"] as? [String:Any] ?? [:]
        Text("历史重试尝试次数："+str(f["historical_retry_attempts"])+"（累计保留） · 当前待处理："+str(current["PENDING"] ?? 0)+" · 当前等待重试："+str(current["RETRY_PENDING"] ?? 0)+" · DB RUNNING："+str(current["RUNNING"] ?? 0))
        Text("Retry 不计入最终失败；任务停止和搜索闭环分别显示。历史资源、历史 Worker 与历史写入数据不会作为当前运行状态。").font(.system(size:11)).foregroundStyle(.secondary)
    }}
}
// A task-owned scheduler has accepted Resume during preparation as well as processing.
private func build151ResumeFailed(lifecycle:String,schedulerActive:Bool)->Bool {
    return !schedulerActive && ["FAILED","FAILED_RESUMABLE","BLOCKED"].contains(lifecycle)
}
private func build151ResumeAccepted(lifecycle:String,schedulerActive:Bool)->Bool {
    return schedulerActive && ["PREPARING","INITIALIZING","STARTING","RUNNING"].contains(lifecycle) || !schedulerActive && lifecycle=="COMPLETED"
}

private struct PhaseCRunning:View {
    @ObservedObject var status:PhaseCReadModel
    @EnvironmentObject var model:ArchiveModel
    var body:some View {Build151StatusPage().onAppear{model.cancelRuntimeSnapshotReads()}}
}

private struct PhaseCHistory: View {
    @EnvironmentObject var model: ArchiveModel
    @State private var refreshed:Date? = nil
    @State private var detail=false
    @StateObject private var runtime=Build151RuntimeViewModel()
    private var terminal:[String:Any] {runtime.facts["terminal_search"] as? [String:Any] ?? [:]}
    private func ended(_ lib:ExistingLibrary)->Bool {lib.isActive && runtime.facts["task_id"] as? String==lib.taskId && terminal["ended"] as? Bool==true && runtime.facts["scheduler_active"] as? Bool==false}
    private var failureDetails: some View {
                    DisclosureGroup("查看失败任务与最后一次错误") {
                        let failures=terminal["retained_failures"] as? [[String:Any]] ?? []
                        ForEach(Array(failures.enumerated()),id:\.offset) { _,failure in
                            VStack(alignment:.leading,spacing:4) {
                                Text(failure["relative_path"] as? String ?? "素材名称未知").font(.headline)
                                Text("阶段：\(failure["stage"] as? String ?? "未知") · 已执行 \(failure["attempts"] as? Int ?? 0) 次 · 最终失败保留")
                                Text((failure["error_type"] as? String ?? "未记录错误类型")+"："+(failure["error_message"] as? String ?? "未记录错误详情")).font(.caption).textSelection(.enabled)
                                DisclosureGroup("任务身份") {Text("\(failure["job_id"] as? String ?? "")\nrevision：\(failure["revision"] as? String ?? "")").font(.caption.monospaced()).textSelection(.enabled)}
                            }.padding(.vertical,6)
                        }
                        if failures.isEmpty {Text("没有可显示的任务错误详情；历史记录未清除。")}
                        if terminal["retained_failures_truncated"] as? Bool==true {Text("此处显示前100项，完整历史仍保留在任务日志。")}
                    }
    }
    var body: some View {
        ScrollView { VStack(alignment:.leading,spacing:14) {
            PageHeader(title:"任务历史",subtitle:"现有项目与历史记录 · 打开项目不会自动处理；已结束项目可直接搜索")
            HStack { PhaseCDisabled(title:"历史任务比对",reason:"完整比较字段尚无 current-safe 投影，不用旧容量数字替代。")
                Spacer();Text(refreshed.map{ "最近刷新："+$0.formatted(date:.omitted,time:.standard) } ?? "尚未刷新").font(.caption)
                Button("刷新列表") { model.loadLibraryCatalog();refreshed=Date() }
            }
            if !model.catalogError.isEmpty {Text(model.catalogError).foregroundStyle(.red)}
            if (model.libraryCatalog).isEmpty && model.catalogError.isEmpty { PhaseCBox(title:"暂无已登记任务") { Text("新建任务后由 library registry 提供真实任务信息。").foregroundStyle(.secondary) } }
            if terminal["ended"] as? Bool == true {
                PhaseCBox(title:(runtime.facts["library_name"] as? String ?? "当前项目")+" · 保留的未完成任务") {
                    Text("已发布 \(terminal["published_sources"] as? Int ?? 0) 个来源；未发布 \(terminal["unpublished_sources"] as? Int ?? 0) 个来源。失败任务不等于失败来源数。")
                    Button("搜索素材") {model.page = .search}.buttonStyle(.borderedProminent)
                    failureDetails
                }
            }
            ForEach(model.libraryCatalog) { lib in
                PhaseCBox(title:lib.taskName) {
                    Text(lib.taskPath).font(.caption.monospaced()).textSelection(.enabled)
                    HStack { Text("图片 \(lib.imageCount) · 视频 \(lib.videoCount)");Spacer();Text(ended(lib) ? ((terminal["unpublished_sources"] as? Int ?? 0)>0 ? "本次处理已结束，部分素材未完成":"本次处理已结束") : lib.status); if lib.isActive {Button("搜索素材") {model.page = .search}.buttonStyle(.borderedProminent)};Button("阶段与索引") { model.loadHistoryDetail(lib);detail=true };Button("打开项目 / Open") { model.activateLibrary(lib) }.accessibilityIdentifier("build151.project.open.\(lib.taskId)") }
                    DisclosureGroup("任务摘要 · 来源闭环 · 索引占用") { Text("完成 / 可搜索 / 索引占用均以 backend 明确返回的事实为准；未返回字段显示 —。").font(.caption) }
                    HStack { Button("打开任务目录") { NSWorkspace.shared.open(URL(fileURLWithPath:lib.taskPath).deletingLastPathComponent()) };Button("查看 task.json") { NSWorkspace.shared.open(URL(fileURLWithPath:lib.taskPath)) };Button("复制任务路径") { NSPasteboard.general.clearContents();NSPasteboard.general.setString(lib.taskPath,forType:.string) };PhaseCDisabled(title:"删除任务",reason:"任务删除安全边界未启用") }
                }
            }
        }.padding(24) }.onAppear{model.loadLibraryCatalog();runtime.refresh()}.onChange(of:model.historyLoading) { loading in if !loading && model.historyDetail != nil {detail=true} }.sheet(isPresented:$detail) { VStack(alignment:.leading,spacing:16) {
            Text("阶段与索引").font(.title2.bold());ScrollView { VStack(alignment:.leading,spacing:10) {
                if let report=model.historyDetail {
                    Text(report.taskName).font(.headline)
                    if runtime.facts["task_id"] as? String == report.taskId && terminal["ended"] as? Bool == true && runtime.facts["scheduler_active"] as? Bool == false {
                        Text((terminal["unpublished_sources"] as? Int ?? 0)>0 ? "本次处理已结束，部分素材未完成" : "本次处理已结束")
                        Text("已发布 \(terminal["published_sources"] as? Int ?? 0) 个来源；未发布 \(terminal["unpublished_sources"] as? Int ?? 0) 个来源。无合法待办，失败历史保留。")
                        Button("搜索素材") {detail=false;model.page = .search}.buttonStyle(.borderedProminent)
                        failureDetails
                        DisclosureGroup("原始历史摘要（保留）") {
                            Text("历史终态：\(report.taskStatus) · \(report.elapsedHuman ?? "历史耗时未记录")")
                            Text(report.pipeline.errorSummary ?? report.error ?? "旧摘要未记录错误；当前错误见上方任务证据").textSelection(.enabled)
                            if let details=report.pipeline.errorDetails {Text(details).font(.caption).textSelection(.enabled)}
                        }
                    } else {
                        Text("历史终态：\(report.taskStatus) · \(report.elapsedHuman ?? "历史耗时未记录")")
                        Text(report.pipeline.errorSummary ?? report.error ?? "请按阶段与日志核查未完成原因").textSelection(.enabled)
                        if let details=report.pipeline.errorDetails {Text(details).font(.caption).textSelection(.enabled)}
                    }
                    if let path=report.pipeline.errorLogPath {Text("错误日志："+path).font(.caption).textSelection(.enabled)}
                } else {Text(model.historyLoading ? "正在读取所选任务历史…" : model.historyError)}
            } };Button("关闭") { detail=false }
        }.padding(24).frame(width:900,height:620) }
    }
}

private struct PhaseCFavorites: View {
    @EnvironmentObject var model:ArchiveModel
    @State private var kind="全部";@State private var rating="全部星级"
    var body: some View { ScrollView { VStack(alignment:.leading,spacing:18) {
        PageHeader(title:"我的收藏",subtitle:"收藏集合与原片在线状态分离；使用与搜索详情相同的本地元数据体系")
        HStack { Button("刷新收藏") {}.disabled(true);Button("全选当前收藏") { model.selectForExport(model.favoriteResults) }.disabled(model.favoriteResults.isEmpty);Button("清空导出选择") { model.clearExportSelection() };Spacer();Button("导出 PDF") { model.exportSelectedPDF() }.disabled(model.selectedExportResults.isEmpty);Button("导出 Excel 可打开的 CSV") { model.exportSelectedCSV() }.disabled(model.selectedExportResults.isEmpty) }
        HStack { Picker("类型",selection:$kind) { ForEach(["全部","视频","图片"],id:\.self){Text($0)} }.frame(width:220);Picker("星级",selection:$rating) { ForEach(["全部星级","5 星","4 星","3 星","2 星","1 星"],id:\.self){Text($0)} }.frame(width:220);Spacer() }
        PhaseCBox(title:"收藏集合暂不可用") { Text("当前尚缺 task-scoped metadata identity 与 current-safe favorite projection，未调用旧 reader。").foregroundStyle(.secondary);Text("已有索引预览与原片在线状态应独立；安全接口建立前不假造收藏记录。").font(.caption) }
    }.padding(24) } }
}

private struct PhaseCGroups: View {
    @State private var tab="重复素材"
    var body: some View { ScrollView { VStack(alignment:.leading,spacing:18) {
        PageHeader(title:"素材分组",subtitle:"完全重复 / 延时摄影 / 连拍候选 · 不删除原片，不恢复旧分组 SQL")
        Picker("分组类型",selection:$tab) { ForEach(["重复素材","延时摄影","连拍组"],id:\.self){Text($0)} }.pickerStyle(.segmented)
        if tab=="重复素材" {
            HStack { ForEach(["推荐清理","全选重复副本","手动选择","选择全部组","清空选择","查看清理计划"],id:\.self) { Button($0) {}.disabled(true) } }
            PhaseCBox(title:"重复素材治理暂不可用") { Text("完全重复仅以内容哈希一致判断；建议保留不等于证明原始文件。");Text("keeper / 数据库与项目引用保护 / rebind / 废纸篓 / 审计链未齐备，清理不启用。").font(.caption).foregroundStyle(.secondary) }
        } else if tab=="延时摄影" { PhaseCBox(title:"延时摄影分组暂不可用") { Text("等待 current-safe representative / group projection。照片数量、时间范围、中位间隔和 first/middle/last 不使用原型数据。").foregroundStyle(.secondary);Button("打开延时摄影组") {}.disabled(true) } }
        else { PhaseCBox(title:"连拍组 · 规则待最终确认") { Text("候选入口保留，不自动归并或清理。");Button("打开候选组") {}.disabled(true);Button("标记为不是连拍") {}.disabled(true) } }
    }.padding(24) } }
}

private struct PhaseCSettings: View {
    @EnvironmentObject var model:ArchiveModel
    @State private var editor=false
    var body: some View { ScrollView { VStack(alignment:.leading,spacing:18) {
        PageHeader(title:"设置",subtitle:"查看本地模型位置、识别词表和处理方式")
        PhaseCBox(title:"处理策略") { Text("画面理解和语音识别各使用一个工作进程。\n不启用收尾加速；系统自动管理资源、失败重试和断点恢复。") }
        PhaseCBox(title:"YOLOE 识别词表") {
            HStack { Text("A · 核心识别词");Spacer();Text("使用当前识别词表");Button("查看词表") { editor=true } }
            HStack { Text("B · 扩展识别词");Spacer();Text(model.snapshot == nil ? "正在获取" : (model.yoloeEnableBExtended ? "开" : "关"));Button("查看词表") { editor=true } }
            Text("核心识别词始终启用，至少需要保留一个词。扩展识别词是否启用以上方显示的状态为准。").font(.caption).foregroundStyle(.secondary)
        }
        PhaseCBox(title:"本地模型位置") { HStack { TextField("模型根目录",text:$model.modelRoot);Button("浏览…") { model.chooseModelRoot() };Button("检查并保存") { model.saveModelRoot() } };Text("不联网下载，不替换生产模型。").font(.caption) }
        PhaseCBox(title:"系统与安全") { Text("原始素材只读\n当前仅整理图片和视频，不处理独立音频\n能够搜索索引不代表原片当前可访问\n未通过安全检查时，禁止改写素材信息") }
        if !model.actionMessage.isEmpty { Text(model.actionMessage).foregroundStyle(model.actionFailed ? .red : .secondary) }
    }.padding(24) }.textFieldStyle(.roundedBorder).sheet(isPresented:$editor) { PhaseCKeywordSheet().environmentObject(model) } }
}
private struct PhaseCKeywordSheet: View {
    @EnvironmentObject var model:ArchiveModel
    @Environment(\.dismiss) private var dismiss
    @State private var layer="A";@State private var search=""
    var body: some View { VStack(alignment:.leading,spacing:14) {
        HStack { Text("YOLOE 中英文词表").font(.title2.bold());Spacer();Button("关闭") { dismiss() } }
        Picker("词层",selection:$layer) { Text("A · 核心词").tag("A");Text("B · 扩展词").tag("B") }.pickerStyle(.segmented)
        TextField("查找英文或中文",text:$search).textFieldStyle(.roundedBorder)
        HStack { Text("英文关键词").frame(width:280,alignment:.leading);Text("中文说明");Spacer() }.font(.headline)
        ScrollView { VStack(alignment:.leading,spacing:6) {
            let text=layer=="A" ? model.yoloeACoreText : model.yoloeBExtendedText
            ForEach(Array(text.split(separator:"\n").map(String.init).filter{search.isEmpty || $0.localizedCaseInsensitiveContains(search)}.enumerated()),id:\.offset) { _,line in
                let pair=line.split(separator:"=",maxSplits:1).map{String($0).trimmingCharacters(in:.whitespaces)}
                HStack { Text(pair.first ?? "").frame(width:280,alignment:.leading);Text(pair.count>1 ? pair[1] : "—");Spacer() }.padding(6).background(Color.secondary.opacity(0.05))
            }
        } }
        Text("当前以只读方式展示真实 profile。窄化的关键词保存边界尚未建立，禁用写操作，避免旧 save-profile 同时覆盖处理策略。").font(.caption).foregroundStyle(.secondary)
        HStack { ForEach(["添加词条","删除词条","导入…","导出…","保存词表与启用状态"],id:\.self) { Button($0) {}.disabled(true) } }
    }.padding(24).frame(width:820,height:600) }
}

if CommandLine.arguments.contains("--check") { runCheckAndExit() }
@_silgen_name("sandbox_init")
func candidateSandboxInit(_ profile: UnsafePointer<CChar>, _ flags: UInt64, _ error: UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>?) -> Int32
// Optional validation envelope; omitted from the formal installed package.
let validationURL = Bundle.main.resourceURL!.appendingPathComponent("build151_local_validation.json")
if FileManager.default.fileExists(atPath: validationURL.path) {
    let validation = try JSONSerialization.jsonObject(with: Data(contentsOf: validationURL)) as! [String:String]
    guard validation["contract"] == "build151_local_validation_v1", let stateRoot = validation["state_root"], stateRoot.hasPrefix("/") else { fatalError("Invalid validation envelope") }
    let profileURL = Bundle.main.resourceURL!.appendingPathComponent("build151_local_validation.sb")
    let profile = try String(contentsOf: profileURL, encoding: .utf8)
    var sandboxError: UnsafeMutablePointer<CChar>? = nil
    let code = profile.withCString { candidateSandboxInit($0, 0, &sandboxError) }
    if code != 0 { fatalError("Validation sandbox failed") }
    setenv("PHASE_C_STATE_ROOT", stateRoot, 1)
    setenv("MEDIA_ARCHIVE_APP_STATE_ROOT", stateRoot, 1)
}
setenv("PYTHONDONTWRITEBYTECODE", "1", 1)

private final class ResponsiveArchiveWindow: NSWindow {
    override var minSize: NSSize {
        get { NSSize(width: max(560, super.minSize.width), height: max(420, super.minSize.height)) }
        set { super.minSize = NSSize(width: max(560, newValue.width), height: max(420, newValue.height)) }
    }
}
private final class ResponsiveWindowDelegate: NSObject, NSWindowDelegate {
    weak var model:ArchiveModel?
    func windowWillClose(_ notification:Notification){model?.stopSearchPreparation()}
    func windowWillResize(_ sender: NSWindow, to frameSize: NSSize) -> NSSize {
        NSSize(width: max(560, frameSize.width), height: max(420, frameSize.height))
    }
    func windowDidEndLiveResize(_ notification: Notification) {
        guard let window = notification.object as? NSWindow else { return }
        build151LifecycleEvent("window_resize_end frame=\(window.frame.width)x\(window.frame.height) content=\(window.contentView?.bounds.width ?? 0)x\(window.contentView?.bounds.height ?? 0) minimum=\(window.minSize.width)x\(window.minSize.height)")
    }
}
private let responsiveWindowDelegate = ResponsiveWindowDelegate()

private func installBuild160StandardMenus(_ application: NSApplication, in mainMenu: NSMenu) {
    let editMenu = NSMenu(title: "编辑")
    let editRoot = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
    editRoot.submenu = editMenu; mainMenu.addItem(editRoot)
    for (title, selector, key) in [("撤销", "undo:", "z"), ("重做", "redo:", "z"),
                                   ("剪切", "cut:", "x"), ("复制", "copy:", "c"),
                                   ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
        if selector == "cut:" { editMenu.addItem(.separator()) }
        let item = NSMenuItem(title: title, action: NSSelectorFromString(selector), keyEquivalent: key)
        item.keyEquivalentModifierMask = selector == "redo:" ? [.command, .shift] : [.command]
        editMenu.addItem(item)
    }
    let windowMenu = NSMenu(title: "窗口")
    let windowRoot = NSMenuItem(title: "窗口", action: nil, keyEquivalent: "")
    windowRoot.submenu = windowMenu; mainMenu.addItem(windowRoot)
    windowMenu.addItem(NSMenuItem(title: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
    windowMenu.addItem(NSMenuItem(title: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
    windowMenu.addItem(NSMenuItem(title: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: ""))
    application.windowsMenu = windowMenu
}

UserDefaults.standard.register(defaults: ["AppleLanguages": ["zh-Hans"], "AppleLocale": "zh_CN"])
let application = NSApplication.shared
application.setActivationPolicy(.regular)
let mainMenu = NSMenu()
let appMenuItem = NSMenuItem()
mainMenu.addItem(appMenuItem)
let appMenu = NSMenu()
private final class Build160AboutController: NSObject {
    @objc func showAbout(_ sender: Any?) {
        let credits = NSMutableAttributedString(string: "开发者：Horizon-94\nGitHub：Horizon-94/local-media-organizer\n项目源码许可：GPL-3.0-only。第三方组件与模型遵循各自许可证。")
        let range = (credits.string as NSString).range(of: "Horizon-94/local-media-organizer")
        credits.addAttribute(.link, value: URL(string: "https://github.com/Horizon-94/local-media-organizer")!, range: range)
        NSApplication.shared.orderFrontStandardAboutPanel(options: [
            .applicationName: bundledAppName,
            .applicationVersion: "\(bundledAppVersion) build\(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "—")",
            .version: "", .credits: credits
        ])
    }
}
private let aboutController = Build160AboutController()
let aboutItem = NSMenuItem(title: "关于本地数据库", action: #selector(Build160AboutController.showAbout(_:)), keyEquivalent: "")
aboutItem.target = aboutController
appMenu.addItem(aboutItem)
appMenu.addItem(NSMenuItem.separator())
let quitItem = NSMenuItem(title: "退出 " + bundledAppName, action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
quitItem.target = application
appMenu.addItem(quitItem)
appMenuItem.submenu = appMenu
installBuild160StandardMenus(application, in: mainMenu)
application.mainMenu = mainMenu
if let root = ProcessInfo.processInfo.environment["PHASE_C_STATE_ROOT"] { setenv("MEDIA_ARCHIVE_APP_STATE_ROOT",root,1) }
let model = ArchiveModel()
let terminationDelegate = ArchiveTerminationDelegate(model)
application.delegate = terminationDelegate
private let window = ResponsiveArchiveWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 900), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
window.delegate = responsiveWindowDelegate
responsiveWindowDelegate.model = model
window.isReleasedWhenClosed = false
terminationDelegate.mainWindow = window
let reopenItem = NSMenuItem(title: "显示主窗口", action: #selector(ArchiveTerminationDelegate.showMainWindow(_:)), keyEquivalent: "0")
reopenItem.target = terminationDelegate
appMenu.insertItem(reopenItem, at: 0)
window.title = bundledAppName; window.titlebarAppearsTransparent = true
private let hosting = NSHostingView(rootView: PhaseCShell().environmentObject(model).environment(\.locale, Locale(identifier: "zh_Hans_CN")).font(.custom("PingFangSC-Regular", size: 13)))
if #available(macOS 13.0, *) { hosting.sizingOptions = [] }
window.contentView = hosting
window.minSize = NSSize(width: 560, height: 420)
window.contentMinSize = NSSize(width: 560, height: 398)
window.center(); window.makeKeyAndOrderFront(nil)
application.activate(ignoringOtherApps: true)
application.run()
