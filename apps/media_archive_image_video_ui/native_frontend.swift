import AppKit
import AVFoundation
import AVKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

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
private let archiveBackground = Color(red: 0.957, green: 0.969, blue: 0.984)
private let archiveMuted = Color(red: 0.40, green: 0.44, blue: 0.52)
private let archiveGreen = Color(red: 0.08, green: 0.62, blue: 0.28)
private let archiveOrange = Color(red: 0.95, green: 0.55, blue: 0.08)
private var bundledAppVersion: String {
    Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "开发版"
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
    let actualScript: String?; let itemsPerSecond: Double?
    let startedWorkers: Int?; let aliveWorkers: Int?; let activeWorkers: Int?
    let idleWorkers: Int?; let crashedWorkers: Int?; let restartCount: Int?
    let queuePending: Int?; let queueRunning: Int?
    let reportPaths: [String: String]?
    var id: String { key }
}
struct PipelineState: Decodable {
    let stages: [PipelineStage]
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
struct DatabaseState: Decodable { let integrityCheck: String; let foreignKeyErrorCount: Int }
struct DatabasePreflight: Decodable {
    let databasePath: String?
    let databaseError: String?
}
struct RuntimeState: Decodable {
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
    let activePid: Int?; let processAlive: Bool; let cpuPercent: Double
    let memoryBytes: Int64; let processCount: Int?; let swapUsedBytes: Int64?; let processState: String?
    let sampleError: String?; let sourceScanned: Bool
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
    var id: String { personClusterId }
}
struct PersonClusterCatalogResponse: Decodable {
    let status: String; let total: Int; let items: [PersonClusterSummary]
    let capabilityNote: String?; let error: String?
}
struct SearchResult: Decodable, Identifiable {
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
    let userAnnotation: UserAssetAnnotation?
    var id: String { resultId ?? UUID().uuidString }
    var exportSelectionId: String {
        resultId ?? "\(sourceContentId ?? "unknown")|\(timecode ?? "00:00")|\(previewPath ?? "")"
    }
}
struct UserAssetAnnotation: Decodable {
    let tags: [String]; let note: String; let favorite: Bool
    let rating: Int; let ignored: Bool; let updatedAt: Double?
}
struct SearchResponse: Decodable {
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
    case search = "搜索素材", favorites = "我的收藏"
    case duplicates = "重复素材", special = "特殊素材", settings = "设置"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .newTask: return "plus.circle"
        case .running: return "play.circle"
        case .history: return "clock"
        case .search: return "magnifyingglass"
        case .favorites: return "heart.fill"
        case .duplicates: return "square.on.square"
        case .special: return "photo.stack"
        case .settings: return "gearshape"
        }
    }
}

final class ArchiveModel: ObservableObject {
    @Published var page: ArchivePage = .newTask
    @Published var snapshot: Snapshot?
    @Published var loadError = ""
    @Published var query = ""
    @Published var mediaType = "全部"
    @Published var previewWindow = "10 秒"
    @Published var searchPathPrefix = ""
    @Published var searchDateFrom = ""
    @Published var searchDateTo = ""
    @Published var searchRequireOCR = false
    @Published var searchRequirePerson = false
    @Published var searching = false
    @Published var searchStatus = ""
    @Published var searchDiagnostic = ""
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
    @Published var actionInProgress = false
    @Published var historyDetail: TaskDetailResponse?
    @Published var historyLoading = false
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
    private var searchElapsedTimer: Timer?
    private var searchStartedAt: Date?
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
    private var refreshTimer: Timer?
    private var lastSnapshotRequestAt = Date.distantPast
    private var snapshotGeneration = 0
    init() {
        loadSnapshot()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            guard let self, self.page == .running || !(self.snapshot?.activeRuns.isEmpty ?? true) else { return }
            let minimumInterval = NSApp.isActive ? 2.0 : 5.0
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
        let helper = helperURL; let config = configURL
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process(); let output = Pipe(); let errors = Pipe()
            process.executableURL = helper
            process.arguments = ["--config", config.path] + arguments
            process.standardOutput = output; process.standardError = errors
            do {
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                let errorData = errors.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let decodedError = try? self.decoder().decode(ErrorResponse.self, from: data)
                let message = process.terminationStatus == 0 ? nil : (
                    decodedError?.displayMessage ?? String(data: errorData, encoding: .utf8) ?? "辅助程序执行失败"
                )
                DispatchQueue.main.async { completion(data, message) }
            } catch { DispatchQueue.main.async { completion(nil, error.localizedDescription) } }
        }
    }

    private func runSearchHelper(
        _ arguments: [String],
        progress: @escaping (SearchProgressEvent) -> Void,
        completion: @escaping (Data?, String?) -> Void
    ) {
        let helper = helperURL; let config = configURL
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process(); let output = Pipe(); let errors = Pipe()
            process.executableURL = helper
            process.arguments = ["--config", config.path] + arguments
            process.standardOutput = output; process.standardError = errors
            do {
                try process.run()
                DispatchQueue.main.async {
                    if self.searching { self.activeSearchProcess = process }
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
                            DispatchQueue.main.async { progress(event) }
                        }
                    }
                    readGroup.leave()
                }
                process.waitUntilExit()
                readGroup.wait()
                lock.lock()
                let finalOutput = outputData
                let finalErrors = errorData
                lock.unlock()
                let decodedError = try? self.decoder().decode(
                    ErrorResponse.self, from: finalOutput
                )
                let message = process.terminationStatus == 0 ? nil : (
                    decodedError?.displayMessage
                    ?? String(data: finalErrors, encoding: .utf8)
                    ?? "辅助程序执行失败"
                )
                DispatchQueue.main.async {
                    if self.activeSearchProcess === process {
                        self.activeSearchProcess = nil
                    }
                    completion(finalOutput, message)
                }
            } catch {
                DispatchQueue.main.async {
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
        searchStartedAt = Date()
        searchElapsedSeconds = 0
        searchElapsedTimer = Timer.scheduledTimer(
            withTimeInterval: 0.2, repeats: true
        ) { [weak self] _ in
            guard let self, let started = self.searchStartedAt else { return }
            self.searchElapsedSeconds = Date().timeIntervalSince(started)
        }
    }

    private func stopSearchTimer() {
        searchElapsedTimer?.invalidate()
        searchElapsedTimer = nil
        if let started = searchStartedAt {
            searchElapsedSeconds = Date().timeIntervalSince(started)
        }
        searchStartedAt = nil
    }

    func cancelSearch() {
        guard searching, let process = activeSearchProcess, process.isRunning else { return }
        searchCancelling = true
        searchStatus = "正在取消本次搜索…"
        process.terminate()
    }

    func prewarmSearch() {
        guard !searchPrewarmAttempted, snapshot?.searchRuntime.ready == true else { return }
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

    func loadSnapshot() {
        snapshotGeneration += 1
        let requestedGeneration = snapshotGeneration
        runHelper(["snapshot"]) { data, error in
            guard requestedGeneration == self.snapshotGeneration else { return }
            guard let data else {
                self.loadError = error ?? "无法读取中心数据库状态"
                return
            }
            do {
                let state = try self.decoder().decode(Snapshot.self, from: data)
                let firstLoad = self.snapshot == nil
                let wasSearchReady = self.snapshot?.pipeline.searchReady == true
                let wasPipelineComplete = self.snapshot?.pipeline.fullPipelineLauncherStatus == "SUCCESS"
                self.snapshot = state; self.loadError = ""
                if firstLoad {
                    self.modelRoot = state.runtimeContract.modelRoot ?? ""
                    if let profile = state.savedProfile {
                        self.modelWorkers = profile.scheduler.modelWorkers
                        self.frameWorkers = profile.scheduler.frameExtractWorkers
                        self.schedulerMode = [
                            "auto": "自动选择（推荐）",
                            "pipeline_async": "数据库流水线异步（尚未开放）",
                            "stage_serial": "按阶段串行",
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
            pendingMessage: "正在切换搜索素材库…",
            successPage: .search,
            clearSearchOnSuccess: true
        )
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

    func saveProfile() {
        let scheduler = ["自动选择（推荐)":"auto", "自动选择（推荐）":"auto", "数据库流水线异步（尚未开放）":"pipeline_async", "按阶段串行":"stage_serial"][schedulerMode] ?? "auto"
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
        searchStatus = "已切换搜索素材库；请输入关键词开始搜索"
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
        guard !actionInProgress else { return }
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

    private func updateSearchPageMarker() {
        nextSearchOffset = (
            searchResults.count < bufferedSearchResults.count
            ? searchResults.count
            : serverNextSearchOffset
        )
    }

    private func showNextBufferedSearchPage() {
        let visibleCount = searchResults.count
        let end = min(visibleCount + 30, bufferedSearchResults.count)
        guard end > visibleCount else { return }
        searchResults.append(
            contentsOf: bufferedSearchResults[visibleCount..<end]
        )
        updateSearchPageMarker()
        searchStatus = "共 \(searchTotalCount) 条可靠结果\(lastSearchMediaSummary) · 当前显示 \(searchResults.count) 条 · 已即时展开缓存结果，未重新搜索"
    }

    private func finishSearchStatus(reused: Bool = false) {
        let elapsed = lastSuccessfulSearchDuration.map {
            " · 用时 \(String(format: "%.1f", $0)) 秒"
        } ?? ""
        searchStatus = searchTotalCount == 0
            ? "没有找到匹配素材"
            : "共 \(searchTotalCount) 条可靠结果\(lastSearchMediaSummary) · 当前显示 \(searchResults.count) 条\(reused ? " · 已即时复用本次结果" : elapsed) · 搜索结果只读，查询历史仅保存在当前素材库"
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
        guard snapshot?.searchRuntime.ready == true else {
            searchStatus = "当前素材库尚未通过搜索预检"
            return
        }
        let clean = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { searchStatus = "请输入要搜索的内容"; return }
        guard let advancedArguments = searchAdvancedArguments() else { return }
        let signature = "\(clean)\u{0}\(mediaType)\u{0}\(previewWindow)\u{0}\(searchPathPrefix)\u{0}\(searchDateFrom)\u{0}\(searchDateTo)\u{0}\(searchRequireOCR)\u{0}\(searchRequirePerson)"
        let continuing = loadMore && signature == lastSearchSignature
        if !loadMore { clearSearchNavigation() }
        if continuing && searchResults.count < bufferedSearchResults.count {
            showNextBufferedSearchPage()
            return
        }
        if !loadMore && signature == lastSearchSignature && !bufferedSearchResults.isEmpty {
            searchResults = Array(bufferedSearchResults.prefix(30))
            updateSearchPageMarker()
            finishSearchStatus(reused: true)
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
            "search", "--query", clean, "--media-type", media,
            "--preview-window-ms", window, "--result-offset", String(offset),
            "--result-limit", "200",
        ] + advancedArguments, progress: { event in
            self.searchProgress = event
            if let elapsed = event.elapsedSeconds {
                self.searchElapsedSeconds = max(self.searchElapsedSeconds, elapsed)
            }
        }) { data, error in
            let wasCancelled = self.searchCancelling
            self.searching = false
            self.searchCancelling = false
            self.stopSearchTimer()
            if wasCancelled {
                self.searchProgress = nil
                self.loadSearchMetadata()
                self.searchStatus = "已取消本次搜索；已有结果未受影响"
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
                self.lastSearchMediaSummary = media == "all"
                    ? " · 图片 \(mediaCounts["image"] ?? 0) 条，视频 \(mediaCounts["video"] ?? 0) 条"
                    : ""
                self.lastSuccessfulSearchDuration = response.elapsedSeconds
                    ?? self.searchElapsedSeconds
                self.showNextBufferedSearchPage()
                self.finishSearchStatus()
                self.searchDiagnostic = ""
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
        runHelper(["search-metadata"]) { data, error in
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
        textView.font = NSFont.systemFont(ofSize: 12)
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
        runHelper(["person-clusters", "--result-offset", "0", "--result-limit", "100"]) { data, error in
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
        guard let path = result.sourcePath, !path.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func open(_ result: SearchResult) {
        guard let path = result.sourcePath, !path.isEmpty else { return }
        let url = URL(fileURLWithPath: path)
        if result.mediaType == "video", let milliseconds = result.previewSegmentStartMs {
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

struct Panel<Content: View>: View {
    let content: Content
    init(@ViewBuilder content: () -> Content) { self.content = content() }
    var body: some View {
        content.padding(20).background(Color.white).clipShape(RoundedRectangle(cornerRadius: 13))
            .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.black.opacity(0.07)))
    }
}

struct PrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(.system(size: 13, weight: .semibold)).foregroundStyle(Color.white)
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
                Text("© 2026 Horizon-94 · GPL-3.0").font(.caption2).foregroundStyle(archiveMuted)
                Text("版本 \(bundledAppVersion)").font(.caption2).foregroundStyle(archiveMuted)
                Text("构建 \(bundledBuildDate)").font(.system(size: 9)).foregroundStyle(archiveMuted)
            }
        }.padding(24).frame(width: 250).background(Color.white.opacity(0.78))
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
                                    ForEach(model.snapshot?.existingLibraries ?? []) { library in
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
                    HStack { Text(state.activeRuns.isEmpty ? "当前没有正在运行的任务" : "正在处理 \(state.activeRuns.count) 个阶段").font(.title3.bold()); Spacer(); Text(String(format: "%.1f%%", state.pipeline.overallPercent)).font(.title2.bold()).foregroundStyle(archiveBlue) }
                    ProgressView(value: state.pipeline.overallPercent, total: 100).tint(archiveBlue)
                    Text(state.pipeline.searchReady ? "图片与视频搜索已经可用" : "搜索将在所需阶段完成后开放").font(.subheadline).foregroundStyle(archiveMuted)
                    if !state.searchRuntime.ready, let error = state.searchRuntime.databasePreflight?.databaseError, !error.isEmpty {
                        Text("搜索数据库不可用：\(error)").font(.caption).foregroundStyle(archiveOrange)
                    }
                    if let eta = state.pipeline.overallEtaSeconds {
                        Text("当前阶段预计剩余 \(formatSeconds(eta))").font(.headline).foregroundStyle(archiveBlue)
                        Text(state.pipeline.overallEtaBasis ?? "按当前剩余工作量动态估算").font(.caption).foregroundStyle(archiveMuted)
                    }
                    HStack {
                        Spacer()
                        if !state.activeRuns.isEmpty {
                            Button("停止当前任务") { model.stopTask() }.foregroundStyle(Color.red)
                        } else if state.pipeline.fullPipelineLauncherStatus == "MAINTENANCE_SEARCH_INCOMPLETE" {
                            Button("继续补齐搜索向量") { model.resumeTask() }.buttonStyle(PrimaryButtonStyle())
                        } else if ["FAILED", "CANCELLED", "FAILED_OUTPUT_ACCEPTANCE"].contains(state.pipeline.fullPipelineLauncherStatus ?? "") {
                            Button("从断点继续") { model.resumeTask() }.buttonStyle(PrimaryButtonStyle())
                        } else if state.pipeline.searchReady {
                            Button("进入搜索素材") { model.page = .search }.buttonStyle(PrimaryButtonStyle())
                        }
                    }
                    } }
                    if state.resources.processAlive {
                        Panel { VStack(alignment: .leading, spacing: 8) {
                            Label("当前子进程资源", systemImage: "gauge.with.dots.needle.67percent").font(.headline)
                            HStack(spacing: 24) {
                                SummaryRow("PID", String(state.resources.activePid ?? 0))
                                SummaryRow("进程树节点", String(state.resources.processCount ?? 1))
                                SummaryRow("CPU 总和（100%=1核）", String(format: "%.1f%%", state.resources.cpuPercent))
                                SummaryRow("进程树 RSS（近似）", formatBytes(state.resources.memoryBytes))
                                SummaryRow("系统 Swap", state.resources.swapUsedBytes.map(formatBytes) ?? "系统未公开")
                            }
                            Text("CPU 与内存汇总当前阶段的完整进程树；Swap 是整台 Mac 的系统值。任务结束后本卡片自动隐藏。")
                                .font(.caption).foregroundStyle(archiveMuted)
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
                        VStack(alignment: .leading) { Text(run.stage ?? "处理中").font(.headline); Text("已用 \(formatSeconds(run.elapsedSeconds)) · 预计剩余 \(formatSeconds(run.etaSeconds))").font(.caption).foregroundStyle(archiveMuted) }
                        Spacer(); VStack(alignment: .trailing) { Text(String(format: "%.1f%%", run.percent ?? 0)).font(.headline); Text("\(run.completed ?? 0) / \(run.total ?? 0)").font(.caption).foregroundStyle(archiveMuted) }
                    } } }
                }
                HStack { MetricCard(title: "图片", value: (state.overview.source["image"]?.count ?? 0).formatted(), icon: "photo"); MetricCard(title: "视频", value: (state.overview.source["video"]?.count ?? 0).formatted(), icon: "video"); MetricCard(title: "可搜索画面", value: state.overview.visualUnitTotalCount.formatted(), icon: "square.grid.3x3"); MetricCard(title: "失败记录", value: (state.pipeline.failedRecordCount ?? 0).formatted(), tint: (state.pipeline.failedRecordCount ?? 0) == 0 ? archiveGreen : .red, icon: "exclamationmark.triangle") }
                    Panel { VStack(alignment: .leading, spacing: 5) {
                    Text("阶段进度（共 \(state.pipeline.stages.count) 个阶段）").font(.headline).padding(.bottom, 8)
                    ForEach(Array(state.pipeline.stages.enumerated()), id: \.element.id) { index, stage in
                        HStack(spacing: 14) {
                            Image(systemName: stage.status == "success" ? "checkmark.circle.fill" : (stage.status == "running" ? "circle.dotted" : (stage.status == "failed" ? "xmark.circle.fill" : "clock.fill"))).font(.title3).foregroundStyle(stage.status == "success" ? archiveGreen : (stage.status == "failed" ? Color.red : archiveOrange))
                            VStack(alignment: .leading, spacing: 3) {
                                Text("\(index + 1). \(stage.name)").font(.headline)
                                Text(stage.errorSummary?.isEmpty == false ? stage.errorSummary! : stage.description)
                                    .font(.caption)
                                    .foregroundStyle(stage.status == "failed" ? Color.red : archiveMuted)
                                    .textSelection(.enabled)
                                if stage.status == "running" {
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
                                    Text(stage.etaSeconds.map { "预计剩余 \(formatSeconds($0))" } ?? (stage.etaBasis ?? "正在估算"))
                                        .font(.caption2).foregroundStyle(archiveMuted)
                                }
                                if stage.status == "success" || stage.status == "failed" {
                                    if (stage.outputFiles ?? 0) > 0 || (stage.stageOutputBytes ?? 0) > 0 {
                                        Text("阶段本地产物 \((stage.outputFiles ?? 0).formatted()) 个文件 · \(formatBytes(stage.stageOutputBytes ?? 0))；中心数据库增长 \(formatBytes(stage.databaseDeltaBytes ?? 0))")
                                            .font(.caption2).foregroundStyle(archiveMuted)
                                    }
                                    if let script = stage.actualScript, !script.isEmpty {
                                        Text("实际脚本：\(URL(fileURLWithPath: script).lastPathComponent)")
                                            .font(.caption2).foregroundStyle(archiveMuted)
                                    }
                                    if let summary = stage.reportPaths?["summary"], !summary.isEmpty {
                                        Button("打开阶段完成报告") {
                                            NSWorkspace.shared.open(URL(fileURLWithPath: summary))
                                        }
                                        .buttonStyle(.link)
                                        .font(.caption2)
                                    }
                                }
                            }
                            Spacer(); VStack(alignment: .trailing) {
                                Text(stage.status == "success" ? "已完成" : (stage.status == "running" ? "进行中" : (stage.status == "failed" ? "失败" : "待处理")))
                                    .foregroundStyle(stage.status == "success" ? archiveGreen : (stage.status == "failed" ? Color.red : archiveOrange))
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
                            Image(systemName: stage.status == "success" ? "checkmark.circle.fill" : "clock.fill").foregroundStyle(stage.status == "success" ? archiveGreen : archiveOrange)
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

struct SearchResultCard: View {
    @EnvironmentObject var model: ArchiveModel; let result: SearchResult
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
    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            Group {
                if let path = result.previewPath, let image = NSImage(contentsOfFile: path) { Image(nsImage: image).resizable().scaledToFill() }
                else { ZStack { Color.gray.opacity(0.15); Image(systemName: "photo").font(.largeTitle).foregroundStyle(archiveMuted) } }
            }.frame(width: 250, height: 150).clipped().clipShape(RoundedRectangle(cornerRadius: 9))
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Image(systemName: result.mediaType == "video" ? "video.fill" : "photo.fill").foregroundStyle(result.mediaType == "video" ? archiveBlue : archiveGreen)
                    Text(URL(fileURLWithPath: result.sourceRelativePath ?? "素材").lastPathComponent).font(.headline)
                    Spacer()
                    Toggle("选入导出", isOn: Binding(
                        get: { model.isSelectedForExport(result) },
                        set: { model.setSelectedForExport(result, selected: $0) }
                    )).toggleStyle(.checkbox)
                    Text(String(format: "综合匹配 %.0f%%", (result.score ?? 0) * 100)).font(.caption).foregroundStyle(archiveBlue)
                }
                if result.mediaType == "video" { Text("命中片段：\(result.previewSegmentStartTimecode ?? "--") – \(result.previewSegmentEndTimecode ?? "--") · 命中点：\(result.timecode ?? "--")").font(.subheadline).foregroundStyle(archiveMuted) }
                if result.audioTranscriptMatch == true {
                    Label(
                        "人声转写命中 · 音频时间 \(audioTimecode(result.audioStartTimeMs)) – \(audioTimecode(result.audioEndTimeMs))",
                        systemImage: "waveform"
                    ).font(.subheadline).foregroundStyle(.purple)
                }
                Text("场景：\(result.environmentLabel ?? "未标注")").font(.caption).padding(.horizontal, 7).padding(.vertical, 4).background(Color.orange.opacity(0.12)).clipShape(RoundedRectangle(cornerRadius: 5))
                Text((result.audioTranscriptMatch == true ? "人声转写：" : "描述证据：") + (result.textPreview ?? "该画面通过全视觉通道召回。"))
                    .font(.subheadline).lineLimit(3)
                if !visibleReasons.isEmpty {
                    Text("命中依据：" + visibleReasons.map { ["exact_text":"文字直接命中", "exact_object_label":"物体标签直接命中", "strong_visual_semantic":"画面语义强匹配", "strong_text_semantic":"描述语义强匹配", "combined_visual_text":"画面与描述共同匹配", "audio_transcript_exact":"音频转写文字直接命中", "audio_transcript_semantic":"音频转写语义匹配", "same_person_reid":"本地人脸特征属于同一人物组", "same_person_track_suggestion":"同一视频中的人脸锚定人体轨迹候选", "user_favorite":"本地收藏", "source_timeline":"同一视频的索引时间轴"][$0] ?? $0 }.joined(separator: "、"))
                        .font(.caption).foregroundStyle(archiveBlue)
                }
                if let labels = result.matchedObjectLabels, !labels.isEmpty {
                    Text("物体标签证据：" + labels.prefix(3).map {
                        "\($0.labelZh ?? $0.label ?? "未知标签")（\(Int(($0.confidence ?? 0) * 100))%）"
                    }.joined(separator: "、"))
                        .font(.caption).foregroundStyle(archiveMuted)
                }
                if let terms = result.matchedTextTerms, !terms.isEmpty {
                    Text((result.audioTranscriptMatch == true ? "音频转写命中词：" : "文字证据：") + terms.prefix(4).joined(separator: "、"))
                        .font(.caption).foregroundStyle(archiveMuted)
                }
                let channelScores = [
                    result.openclipCosine.map { "画面原始相似度 \(String(format: "%.3f", $0))" },
                    result.textSemanticScore.map { "描述原始相似度 \(String(format: "%.3f", $0))" },
                ].compactMap { $0 }
                if !channelScores.isEmpty {
                    Text(channelScores.joined(separator: " · ") + "；综合匹配仅用于结果排序，不是识别概率。")
                        .font(.caption2).foregroundStyle(archiveMuted)
                }
                if let clusters = result.personClusters, !clusters.isEmpty {
                    ForEach(clusters) { cluster in
                        HStack {
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
                                }.buttonStyle(.borderless)
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
                Text("所属位置：\(result.sourceRelativePath ?? "")").font(.caption2).foregroundStyle(archiveMuted)
                if result.visualUnitId?.isEmpty == false {
                    DisclosureGroup("人工人物归类") {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("机器没有识别到正脸也可以人工归类；一张画面可加入多个人物，结果只保存在本机。")
                                .font(.caption).foregroundStyle(archiveMuted)
                            HStack {
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
                                }.disabled(manualPersonTargetId.isEmpty)
                            }
                            HStack {
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
                                }
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
                        HStack {
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
                HStack {
                    Button(result.mediaType == "video" ? "播放命中片段" : "打开图片") {
                        model.open(result)
                    }.buttonStyle(PrimaryButtonStyle()).disabled(result.canOpenOriginal != true)
                    Button("在 Finder 中显示") { model.reveal(result) }
                        .disabled(result.canOpenOriginal != true)
                    if result.mediaType == "video", let sourceId = result.sourceContentId {
                        Button("浏览该视频全部画面") { model.browseSourceFrames(sourceId) }
                            .buttonStyle(.bordered)
                    }
                }
            }
        }.padding(14).background(Color.white).clipShape(RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.black.opacity(0.07)))
    }
}

struct SearchPage: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View { ScrollView { VStack(alignment: .leading, spacing: 18) {
        PageHeader(title: "搜索素材", subtitle: "搜索当前选中的素材库；可在任务历史中切换到另一个已完成项目。")
        if let activeLibrary = model.snapshot?.existingLibraries.first(where: { $0.isActive }) {
            Label("当前搜索库：\(activeLibrary.taskName)（图片 \(activeLibrary.imageCount)，视频 \(activeLibrary.videoCount)）", systemImage: "externaldrive.fill")
                .font(.subheadline).foregroundStyle(archiveBlue)
        }
        if model.searchNavigationDepth > 0 {
            Panel { HStack(spacing: 12) {
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
            HStack { Image(systemName: "magnifyingglass").foregroundStyle(archiveMuted); TextField("例如：夜间户外戴眼镜的人物", text: $model.query).textFieldStyle(.plain).font(.title3).onSubmit { model.search() }; Button(model.searching ? "搜索中…" : "搜索") { model.search() }.buttonStyle(PrimaryButtonStyle()).disabled(model.searching || model.snapshot?.searchRuntime.ready != true) }
            Divider(); HStack {
                Picker("素材类型", selection: $model.mediaType) { ForEach(["全部", "视频", "图片", "音频（人声转写）"], id: \.self) { Text($0) } }.frame(width: 220)
                Picker("预览区间", selection: $model.previewWindow) { ForEach(["5 秒", "10 秒"], id: \.self) { Text($0) } }.frame(width: 180)
                Spacer()
            }
            DisclosureGroup("高级过滤（由数据库查询执行）") {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        TextField("文件夹相对路径前缀", text: $model.searchPathPrefix)
                            .textFieldStyle(.roundedBorder)
                        TextField("开始日期 YYYY-MM-DD", text: $model.searchDateFrom)
                            .textFieldStyle(.roundedBorder).frame(width: 180)
                        TextField("结束日期 YYYY-MM-DD", text: $model.searchDateTo)
                            .textFieldStyle(.roundedBorder).frame(width: 180)
                    }
                    HStack {
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
                    HStack {
                        TextField("保存名称", text: $model.savedSearchName)
                            .textFieldStyle(.roundedBorder).frame(maxWidth: 240)
                        Button("保存当前搜索") { model.saveCurrentSearch() }
                            .disabled(model.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        Button("刷新") { model.loadSearchMetadata() }
                        Spacer()
                    }
                    if !model.savedSearches.isEmpty {
                        Text("已保存").font(.caption.bold()).foregroundStyle(archiveMuted)
                        ForEach(model.savedSearches.prefix(8)) { item in
                            Button("\(item.displayName)：\(item.queryText)") {
                                model.applySearchMetadata(item.queryText, filters: item.filters)
                            }.buttonStyle(.plain).foregroundStyle(archiveBlue)
                        }
                    }
                    if !model.searchHistory.isEmpty {
                        Text("最近搜索").font(.caption.bold()).foregroundStyle(archiveMuted)
                        ForEach(model.searchHistory.prefix(8)) { item in
                            Button("\(item.queryText) · \(item.resultCount) 条 · \(String(format: "%.1f", item.elapsedSeconds)) 秒") {
                                model.applySearchMetadata(item.queryText, filters: item.filters)
                            }.buttonStyle(.plain).foregroundStyle(.primary)
                        }
                    }
                    if !model.searchMetadataStatus.isEmpty {
                        Text(model.searchMetadataStatus).font(.caption).foregroundStyle(archiveMuted)
                    }
                }.padding(.top, 8)
            }
            Divider()
            HStack(spacing: 12) {
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
                    }
                    .buttonStyle(.bordered)
                    .help("只读取同一视频中的现有人脸和人体框；结果需人工确认，不会自动合并人物")
                    .disabled(model.selectedPersonClusterId.isEmpty || model.searching)
                    Spacer()
                }
            }
            if !model.selectedPersonClusterId.isEmpty {
                Divider()
                DisclosureGroup("本地人物名称、标签与人工合并") {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("这些修改只保存在本机，不改写机器识别记录；把多个机器组归到同一人物后仍可取消合并。")
                            .font(.caption).foregroundStyle(archiveMuted)
                        HStack {
                            TextField("人物名称，例如：张老师", text: $model.personDisplayName)
                                .textFieldStyle(.roundedBorder).frame(maxWidth: 260)
                            TextField("标签，用逗号分隔", text: $model.personTags)
                                .textFieldStyle(.roundedBorder).frame(maxWidth: 300)
                            Button("保存名称和标签") { model.savePersonName() }
                        }
                        HStack {
                            Picker("归入", selection: $model.personMergeTargetId) {
                                Text("请选择另一个人物").tag("")
                                ForEach(model.personClusterCatalog.filter { $0.personClusterId != model.selectedPersonClusterId }) { person in
                                    Text(person.displayName).tag(person.personClusterId)
                                }
                            }.frame(maxWidth: 330)
                            Button("确认属于同一人物") { model.mergeSelectedPerson() }
                                .disabled(model.personMergeTargetId.isEmpty)
                            Button("取消该人物的人工合并") { model.detachSelectedPerson() }
                            Spacer()
                        }
                        if !model.personEditStatus.isEmpty {
                            Text(model.personEditStatus).font(.caption).foregroundStyle(archiveBlue)
                        }
                    }.padding(.top, 8)
                }
            }
        } }
        Text(model.snapshot?.searchRuntime.ready == true ? "全量视觉搜索已启用；搜索结果只读，查询向量不持久化；搜索历史和保存查询仅写入当前素材库的本地用户元数据。" : (model.snapshot?.searchRuntime.databasePreflight?.databaseError.flatMap { $0.isEmpty ? nil : "搜索数据库不可用：\($0)" } ?? "首次素材整理完成后，搜索会在这里自动开放。"))
            .font(.subheadline).foregroundStyle(archiveBlue).padding(12).frame(maxWidth: .infinity, alignment: .leading).background(Color.blue.opacity(0.08)).clipShape(RoundedRectangle(cornerRadius: 8))
        HStack(spacing: 7) {
            Image(systemName: model.searchPrewarmReady ? "bolt.fill" : "bolt")
                .foregroundStyle(model.searchPrewarmReady ? archiveGreen : archiveMuted)
            Text(model.searchPrewarmStatus).font(.caption).foregroundStyle(archiveMuted)
        }
        Text("搜索会扫描全量画面向量，并融合 AI 描述、OCR 文字和物体标签。宽泛词与具体描述会重新排序；同一素材的不同时间点仍是独立画面。")
            .font(.caption).foregroundStyle(archiveMuted)
        if !model.personCapabilityNote.isEmpty {
            Label(model.personCapabilityNote, systemImage: "person.crop.circle.badge.questionmark")
                .font(.caption).foregroundStyle(archiveMuted)
        }
        if model.searching, let progress = model.searchProgress {
            Panel { VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Label("正在搜索“\(model.query.trimmingCharacters(in: .whitespacesAndNewlines))”", systemImage: "magnifyingglass")
                        .font(.headline)
                    Spacer()
                    Text("阶段 \(progress.stageIndex) / \(progress.totalStages)")
                        .font(.caption.bold()).foregroundStyle(archiveBlue)
                    Text(String(format: "%.1f 秒", model.searchElapsedSeconds))
                        .font(.caption.monospacedDigit()).foregroundStyle(archiveMuted)
                    Button(model.searchCancelling ? "正在取消…" : "取消") {
                        model.cancelSearch()
                    }
                    .disabled(model.searchCancelling)
                }
                ProgressView(
                    value: Double(progress.stageIndex),
                    total: Double(progress.totalStages)
                ).tint(archiveBlue)
                Text(progress.message).font(.subheadline.bold())
                if let detail = progress.detail, !detail.isEmpty {
                    Text(detail).font(.caption).foregroundStyle(archiveMuted)
                }
                HStack(spacing: 18) {
                    if let completed = progress.completed, let total = progress.total, total > 0 {
                        Text("当前进度 \(completed) / \(total)")
                    }
                    if let overview = model.snapshot?.overview {
                        Text("搜索池：\(overview.visualUnitTotalCount) 个画面 · \(overview.recognition.textVectors) 个文本向量")
                    }
                }.font(.caption).foregroundStyle(archiveMuted)
                ForEach(0..<3, id: \.self) { _ in
                    HStack(spacing: 14) {
                        RoundedRectangle(cornerRadius: 8)
                            .fill(Color.gray.opacity(0.12)).frame(width: 150, height: 82)
                        VStack(alignment: .leading, spacing: 8) {
                            RoundedRectangle(cornerRadius: 4)
                                .fill(Color.gray.opacity(0.14)).frame(width: 240, height: 12)
                            RoundedRectangle(cornerRadius: 4)
                                .fill(Color.gray.opacity(0.10)).frame(maxWidth: .infinity).frame(height: 10)
                            RoundedRectangle(cornerRadius: 4)
                                .fill(Color.gray.opacity(0.10)).frame(width: 300, height: 10)
                        }
                    }
                }
            } }
        }
        if !model.searchStatus.isEmpty { Text(model.searchStatus).font(.subheadline).foregroundStyle(archiveMuted) }
        if model.searching {
            Text("本次扫描统计：正在计算当前搜索范围…")
                .font(.caption).foregroundStyle(archiveMuted)
        } else if let coverage = model.searchCoverage {
            Text("本次已扫描：画面向量 \(coverage.scannedVisualVectorCount) / \(coverage.eligibleVisualUnitCount) · 文本向量 \(coverage.scannedTextVectorCount)")
                .font(.caption).foregroundStyle(archiveMuted)
        }
        if !model.searchDiagnostic.isEmpty {
            DisclosureGroup("展开诊断") {
                VStack(alignment: .leading, spacing: 8) {
                    Text(model.searchDiagnostic).font(.caption.monospaced()).textSelection(.enabled)
                    Button("复制诊断信息") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(model.searchDiagnostic, forType: .string)
                    }
                }.padding(.top, 8)
            }.padding(12).background(Color.red.opacity(0.05)).clipShape(RoundedRectangle(cornerRadius: 8))
        }
        if model.searchResults.isEmpty, let overview = model.snapshot?.overview, model.snapshot?.configurationState == "configured" {
            HStack { MetricCard(title: "可搜索画面", value: overview.visualUnitTotalCount.formatted()); MetricCard(title: "图片素材", value: (overview.source["image"]?.count ?? 0).formatted()); MetricCard(title: "视频素材", value: (overview.source["video"]?.count ?? 0).formatted()); MetricCard(title: "文本向量", value: overview.recognition.textVectors.formatted()) }
        } else if model.snapshot?.configurationState != "configured" {
            Panel { VStack(spacing: 12) { Image(systemName: "magnifyingglass.circle").font(.system(size: 42)).foregroundStyle(archiveMuted); Text("当前还没有可搜索的素材").font(.title3.bold()); Text("请先新建任务并完成第一次整理。").foregroundStyle(archiveMuted) }.frame(maxWidth: .infinity).padding(.vertical, 48) }
        }
        ForEach(model.searchResults) { SearchResultCard(result: $0) }
        if model.nextSearchOffset != nil {
            HStack { Spacer(); Button(model.searching ? "加载中…" : "加载更多") { model.search(loadMore: true) }.buttonStyle(PrimaryButtonStyle()).disabled(model.searching); Spacer() }.padding(.vertical, 12)
        }
    }.padding(34) }.onAppear {
        if model.personClusterCatalog.isEmpty { model.loadPersonClusters() }
        model.prewarmSearch()
    } }
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
    var body: some View { ScrollView { VStack(alignment: .leading, spacing: 18) {
        PageHeader(title: "处理设置", subtitle: "先看电脑能力，再决定并发、抽帧密度和高价值分析范围。")
        if let state = model.snapshot {
            HStack { MetricCard(title: "芯片", value: state.hardware.chip, tint: archiveBlue, icon: "cpu"); MetricCard(title: "CPU", value: "\(state.hardware.cpuCoresTotal) 核"); MetricCard(title: "GPU", value: state.hardware.gpuCores.map { "\($0) 核" } ?? "未公开"); MetricCard(title: "统一内存", value: state.hardware.unifiedMemoryGb.map { String(format: "%.0f GB", $0) } ?? "未公开") }
            Panel { VStack(alignment: .leading, spacing: 13) {
                Text("新任务处理方案").font(.title3.bold())
                Text("默认值按本机能力保守推荐。遇到内存压力时只能自动降低并发，不会静默提高。").font(.subheadline).foregroundStyle(archiveMuted)
                Divider()
                SettingPicker(title: "运行方式", selection: $model.schedulerMode, values: ["自动选择（推荐）", "数据库流水线异步（尚未开放）", "按阶段串行"])
                Label("数据库流水线异步尚未开放：当前只能保存未来方案，不会启动异步总编排器。", systemImage: "clock.badge.exclamationmark")
                    .font(.caption).foregroundStyle(archiveOrange)
                SettingStepper(title: "模型并发路数", value: $model.modelWorkers, range: 1...8, hint: "保守推荐 \(state.hardware.recommendation.modelWorkers) 路 · 估算上限 \(state.hardware.recommendation.estimatedMaxModelWorkers) 路")
                SettingStepper(title: "抽帧并发路数", value: $model.frameWorkers, range: 1...16, hint: "推荐 \(state.hardware.recommendation.frameExtractWorkers) 路")
                SettingPicker(title: "视频抽帧间隔", selection: $model.frameInterval, values: ["1 秒", "2 秒", "3 秒", "4 秒", "5 秒"])
                SettingPicker(title: "高价值分析密度", selection: $model.highValueMode, values: ["兼容当前规则", "目标 15%", "目标 20%", "目标 30%"])
                SettingPicker(title: "图片分析范围", selection: $model.imageScope, values: ["按当前规则筛选图片", "所有普通图片都进入画面描述"])
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
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.black.opacity(0.12)))
                    Text("B 层扩展辅助词").font(.headline)
                    TextEditor(text: $model.yoloeBExtendedText)
                        .font(.system(.body, design: .monospaced))
                        .frame(minHeight: 190)
                        .padding(6)
                        .background(Color.white)
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
                .font(.subheadline).foregroundStyle(archiveMuted).padding(14).frame(maxWidth: .infinity, alignment: .leading).background(Color.white.opacity(0.72)).clipShape(RoundedRectangle(cornerRadius: 10))
            Text("系统与安全").font(.title3.bold())
            HStack(alignment: .top, spacing: 12) {
                SafetyCard(title: "中心数据库", detail: state.configurationState == "configured" ? (state.database.integrityCheck == "ok" ? "完整性检查正常，外键错误 \(state.database.foreignKeyErrorCount)" : "需要检查") : "首次任务创建后建立", passed: state.database.integrityCheck == "ok")
                SafetyCard(title: "全视觉搜索", detail: state.searchRuntime.ready ? "冻结入口 Stop03-5E V2 可用" : "首次整理完成后开放", passed: state.searchRuntime.ready)
                SafetyCard(title: "原始素材保护", detail: "只读；仅在用户主动打开时访问", passed: true)
            }
            Panel { VStack(alignment: .leading, spacing: 8) { Text("模型更新闸门").font(.headline); Text("登记模型指纹  →  离线小样本测试  →  与当前模型对比  →  人工确认  →  明确启用").font(.subheadline).foregroundStyle(archiveMuted); Text("应用不会联网下载模型，也不会自动替换正式模型。").font(.caption).foregroundStyle(archiveBlue) } }
            if !model.actionMessage.isEmpty { Label(model.actionMessage, systemImage: model.actionFailed ? "xmark.circle" : "checkmark.circle").font(.subheadline).foregroundStyle(model.actionFailed ? Color.red : archiveGreen) }
        } else {
            Panel { VStack(alignment: .leading, spacing: 10) {
                Label("正在读取本机能力与任务状态", systemImage: "gearshape.2")
                if !model.loadError.isEmpty { Text(model.loadError).foregroundStyle(Color.red) }
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

struct RootView: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View {
        HStack(spacing: 0) {
            Sidebar(); Divider()
            Group {
                switch model.page {
                case .newTask: NewTaskPage()
                case .running: RunningPage()
                case .history: HistoryPage()
                case .search: SearchPage()
                case .favorites: FavoritesPage()
                case .duplicates: DuplicatesPage()
                case .special: SpecialPage()
                case .settings: SettingsPage()
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
        }.background(archiveBackground).frame(minWidth: 1180, minHeight: 760)
    }
}

func runCheckAndExit() -> Never {
    let helper = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/素材大整理Python")
    let config = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/app_config.json")
    let process = Process(); let output = Pipe(); let errors = Pipe()
    process.executableURL = helper; process.arguments = ["--config", config.path, "snapshot"]
    process.standardOutput = output; process.standardError = errors
    do { try process.run() } catch {
        fputs("FAIL helper launch: \(error)\n", stderr); exit(70)
    }
    let data = output.fileHandleForReading.readDataToEndOfFile()
    let errorData = errors.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
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

if CommandLine.arguments.contains("--check") { runCheckAndExit() }
let application = NSApplication.shared
application.setActivationPolicy(.regular)
let model = ArchiveModel()
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1460, height: 900), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
window.title = bundledAppName; window.titlebarAppearsTransparent = true
window.contentView = NSHostingView(rootView: RootView().environmentObject(model))
window.center(); window.makeKeyAndOrderFront(nil)
application.activate(ignoringOtherApps: true)
application.run()
