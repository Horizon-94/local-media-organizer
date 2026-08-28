import AppKit
import AVFoundation
import AVKit
import Foundation
import ImageIO
import PDFKit
import SwiftUI
import UniformTypeIdentifiers

private final class VideoPreviewWindowController: NSWindowController {
    var readinessObservation: NSKeyValueObservation?
    var player: AVPlayer?
    var onClosed: (() -> Void)?
    private var closeObserver: NSObjectProtocol?

    override init(window: NSWindow?) {
        super.init(window: window)
        if let window {
            closeObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification,
                object: window,
                queue: .main
            ) { [weak self] _ in
                guard let self else { return }
                self.stopPlayback()
                self.window?.contentView = nil
                let callback = self.onClosed; self.onClosed = nil
                callback?()
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

// Editorial previews read the original media. They do not render/transcode a
// temporary clip or mutate the candidate, its selection, or its export range.
private final class EditorialPlayback: ObservableObject {
    let player = AVPlayer()
    let url: URL
    @Published var inPoint: Double
    @Published var outPoint: Double
    @Published var cutLocked: Bool
    @Published var currentSeconds = 0.0
    @Published var durationSeconds = 0.0
    var range: [Double] { [inPoint, outPoint] }
    let onConfirm: (([Double], String) -> String?)?
    @Published var message = "正在读取原片，准备定位入点……"
    @Published var ready = false
    private var observation: NSKeyValueObservation?
    private var notifications: [NSObjectProtocol] = []
    private var stopped = false
    private var segmentMode = true
    private var seekGeneration = UUID()
    private var timeObserver: Any?

    init(url: URL, range: [Double], context: Bool, locked: Bool = false,
         onConfirm: (([Double], String) -> String?)? = nil) {
        self.url = url; self.inPoint = range[0]; self.outPoint = range[1]
        self.cutLocked = locked; self.onConfirm = onConfirm
        let item = AVPlayerItem(url: url)
        player.replaceCurrentItem(with: item)
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.1, preferredTimescale: 60000), queue: .main) { [weak self] time in
            guard time.seconds.isFinite else { return }
            self?.currentSeconds = time.seconds
        }
        observation = item.observe(\.status, options: [.initial, .new]) { [weak self] item, _ in
            DispatchQueue.main.async {
                guard let self, !self.stopped else { return }
                if item.status == .readyToPlay {
                    self.observation?.invalidate(); self.observation = nil
                    self.ready = true
                    self.durationSeconds = item.duration.seconds.isFinite ? item.duration.seconds : 0
                    self.play(context: context)
                } else if item.status == .failed {
                    self.message = "原片无法在内置播放器解码；可尝试用其他播放器打开，或在达芬奇中查看。\n\(item.error?.localizedDescription ?? "未知媒体错误")"
                }
            }
        }
        notifications.append(NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime, object: item, queue: .main
        ) { [weak self] _ in
            guard let self, !self.stopped else { return }
            self.message = self.segmentMode ? "建议片段播放结束。可重播，或查看原片前后文。" : "已播放到原片结尾。"
        })
        notifications.append(NotificationCenter.default.addObserver(
            forName: .AVPlayerItemFailedToPlayToEndTime, object: item, queue: .main
        ) { [weak self] _ in
            self?.player.pause()
            self?.message = "原片播放中断：请检查素材盘连接，或用其他播放器打开原片。"
        })
    }

    func play(context: Bool) {
        guard !stopped, ready, let item = player.currentItem else { return }
        player.pause()
        seekGeneration = UUID()
        let generation = seekGeneration
        let duration = item.duration.seconds
        let validRange = range.count == 2 && range[0].isFinite && range[1].isFinite && range[0] >= 0 && range[1] > range[0]
        guard duration.isFinite, duration > 0 else {
            message = "无法读取原片时长；请用其他播放器打开，或在达芬奇中查看。"; return
        }
        guard context || (validRange && range[1] <= duration + 0.001) else {
            message = String(format: "建议出入点无效或超过原片长度（%.2f 秒）。请修改候选箱出入点，或点击查看原片前后文。", duration); return
        }
        segmentMode = !context
        let start = context ? min(max(0, validRange ? range[0] - 3 : 0), max(0, duration - 0.1)) : range[0]
        item.forwardPlaybackEndTime = context ? .invalid : CMTime(seconds: range[1], preferredTimescale: 60000)
        message = "正在定位原片……"
        item.seek(to: CMTime(seconds: start, preferredTimescale: 60000), toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] finished in
            DispatchQueue.main.async {
                guard let self, !self.stopped, self.seekGeneration == generation else { return }
                guard finished else { self.message = "定位失败，请重播或用其他播放器打开原片。"; return }
                self.message = context
                    ? "原片前后文：从入点前约 3 秒开始，不在建议出点停止；可拖动进度条查看全片。"
                    : String(format: "播放建议片段：%.2f → %.2f 秒（%.2f 秒），到出点自动停止。", self.range[0], self.range[1], self.range[1] - self.range[0])
                self.player.play()
            }
        }
    }

    func seekPreview(to seconds: Double) {
        guard !stopped, ready, seconds.isFinite, let item = player.currentItem else { return }
        player.pause(); seekGeneration = UUID()
        let generation = seekGeneration
        let position = min(max(0, seconds), durationSeconds)
        item.forwardPlaybackEndTime = .invalid
        item.seek(to: CMTime(seconds: position, preferredTimescale: 60000), toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] finished in
            DispatchQueue.main.async {
                guard let self, !self.stopped, self.seekGeneration == generation, finished else { return }
                self.currentSeconds = self.player.currentTime().seconds
                self.message = String(format: "已定位 %.2f 秒；可设为入点或出点，也可填写下面的秒数。", self.currentSeconds)
            }
        }
    }

    func markPoint(isIn: Bool) {
        guard !cutLocked, !stopped, ready else { return }
        player.pause()
        let position = player.currentTime().seconds
        guard position.isFinite else { return }
        if isIn { inPoint = position } else { outPoint = position }
        message = "已标记\(isIn ? "入点" : "出点")；先试播，再确认锁定。关闭窗口不会保存未确认的调整。"
    }

    func confirmCut(decision: String) {
        guard !stopped, ready, !cutLocked, let onConfirm else { return }
        guard inPoint.isFinite, outPoint.isFinite, inPoint >= 0, outPoint > inPoint,
              outPoint <= durationSeconds + 0.001 else {
            message = "无法锁定：出点必须晚于入点，而且不能超过原片长度。"; return
        }
        if let error = onConfirm(range, decision) { message = error; return }
        cutLocked = true
        message = "剪点已锁定并\(decision == "selected" ? "加入主选" : "加入备选")；候选箱和导出将使用此范围。需要改动时可解锁调整。"
    }

    func unlockDraft() {
        cutLocked = false
        message = "正在修改副本；重新确认前，候选箱仍保留上次锁定的剪点。关闭窗口则放弃本次调整。"
    }

    func stop() {
        guard !stopped else { return }
        stopped = true; ready = false
        seekGeneration = UUID()
        observation?.invalidate(); observation = nil
        if let token = timeObserver { player.removeTimeObserver(token); timeObserver = nil }
        player.currentItem?.cancelPendingSeeks()
        player.currentItem?.asset.cancelLoading()
        player.pause(); player.replaceCurrentItem(with: nil)
        for token in notifications { NotificationCenter.default.removeObserver(token) }
        notifications = []
    }
    deinit { stop() }
}

private struct EditorialPlayerSurface: NSViewRepresentable {
    let player: AVPlayer
    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView(); view.player = player; view.controlsStyle = .floating
        return view
    }
    func updateNSView(_ view: AVPlayerView, context: Context) { view.player = player }
    static func dismantleNSView(_ view: AVPlayerView, coordinator: ()) { view.player = nil }
}

private struct EditorialPlayerPanel: View {
    @ObservedObject var playback: EditorialPlayback
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            EditorialPlayerSurface(player: playback.player).frame(minWidth: 640, minHeight: 280)
            Text(playback.message).font(.callout).textSelection(.enabled)
            if playback.onConfirm != nil {
                EditorialTrimControls(playback: playback)
            }
            HStack {
                Button("重播建议片段") { playback.play(context: false) }.disabled(!playback.ready)
                Button("查看原片前后文") { playback.play(context: true) }.disabled(!playback.ready)
                Button("用其他播放器打开") { NSWorkspace.shared.open(playback.url) }
                Button("定位原文件") { NSWorkspace.shared.activateFileViewerSelecting([playback.url]) }
            }
            Text(playback.url.path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            Text("只读原片，不转码。调整在确认前只是草稿；灰片保持原始色彩。")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(12)
    }
}

private struct EditorialTrimControls: View {
    @ObservedObject var playback: EditorialPlayback
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(String(format: "当前位置 %.2f / %.2f 秒", playback.currentSeconds, playback.durationSeconds)).font(.caption).monospacedDigit()
                Slider(value: Binding(get: { playback.currentSeconds }, set: { playback.seekPreview(to: $0) }),
                       in: 0...max(0.001, playback.durationSeconds)).disabled(!playback.ready)
            }
            HStack {
                Button("当前位置设为入点") { playback.markPoint(isIn: true) }
                Button("当前位置设为出点") { playback.markPoint(isIn: false) }
                Text("入点秒")
                TextField("入点", value: $playback.inPoint, format: .number.precision(.fractionLength(2))).frame(width: 75)
                Text("出点秒")
                TextField("出点", value: $playback.outPoint, format: .number.precision(.fractionLength(2))).frame(width: 75)
                Text(String(format: "使用 %.2f 秒", playback.outPoint - playback.inPoint)).monospacedDigit()
            }.disabled(!playback.ready || playback.cutLocked).textFieldStyle(.roundedBorder)
            HStack {
                if playback.cutLocked {
                    Label("已确认并锁定剪点", systemImage: "lock.fill")
                    Button("解锁调整") { playback.unlockDraft() }
                } else {
                    Button("锁定剪点并入选") { playback.confirmCut(decision: "selected") }
                    Button("锁定剪点并备选") { playback.confirmCut(decision: "review") }
                    Text("先用“重播建议片段”试听，确认后才更新候选箱。")
                }
            }.disabled(!playback.ready)
        }.font(.caption).padding(8).background(Color.blue.opacity(0.06)).cornerRadius(6)
    }
}

private final class EditorialPlaybackWindowController: NSWindowController {
    let playback: EditorialPlayback
    var onClosed: (() -> Void)?
    private var closeObserver: NSObjectProtocol?
    init(url: URL, range: [Double], context: Bool, locked: Bool = false,
         onConfirm: (([Double], String) -> String?)? = nil) {
        playback = EditorialPlayback(url: url, range: range, context: context, locked: locked, onConfirm: onConfirm)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1020, height: 720),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "文稿选片预览 · \(url.lastPathComponent)"
        window.contentView = NSHostingView(rootView: EditorialPlayerPanel(playback: playback))
        window.center()
        super.init(window: window)
        closeObserver = NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: window, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.playback.stop()
            self.window?.contentView = nil
            let callback = self.onClosed; self.onClosed = nil
            callback?()
        }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    deinit {
        playback.stop()
        if let closeObserver { NotificationCenter.default.removeObserver(closeObserver) }
    }
}

private struct EditorialPreviewResponse: Decodable {
    let sourcePath: String
    let mediaType: String
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
    let date = Bundle.main.object(forInfoDictionaryKey: "HorizonBuildDate") as? String ?? "开发构建"
    let hotfix = Bundle.main.object(forInfoDictionaryKey: "HorizonHotfixLabel") as? String ?? ""
    return hotfix.isEmpty ? date : "\(date) · \(hotfix)"
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
// Match by database identity, never by the common filename media_archive.sqlite.
// No filesystem reads or metadata writes are needed to show a registered name.
func editorialLibraryDisplayName(database: String, libraries: [ExistingLibrary]) -> String {
    guard !database.isEmpty else { return "尚未连接素材库" }
    let target = URL(fileURLWithPath: database).standardizedFileURL
    guard let library = libraries.first(where: {
        URL(fileURLWithPath: $0.database).standardizedFileURL == target
    }) else { return "素材库名称未登记（请在任务历史核对）" }
    return "\(library.taskName)｜图片 \(library.imageCount)｜视频 \(library.videoCount)"
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
    // Legacy rows may lack result_id. A random ID recreated the entire card
    // (and its edit state) on every progress tick or layout update.
    var id: String {
        if let resultId, !resultId.isEmpty { return resultId }
        return "legacy|\(exportSelectionId)|\(visualUnitId ?? "")|\(audioEvidenceId ?? "")|\(audioStartTimeMs ?? -1)|\(audioEndTimeMs ?? -1)"
    }
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

struct EditorialCandidate: Codable, Identifiable {
    let candidateId: String
    let sourceContentId: String
    let sourceFile: String
    let mediaType: String
    let previewPath: String
    let displayTitle: String
    let description: String
    let pool: String
    let role: String
    let shortlistRank: Int
    let recommendation: String
    let evidenceMode: String
    let anchorTimeMs: Int?
    let startMs: Int
    let endMs: Int
    let timeBasis: String
    let matchReasons: [String]
    let risks: [String]
    let sentenceNeed: String
    let candidateContribution: String
    let fitReason: String
    let visualLanguage: String
    let fitBoundary: String
    let acceptanceCheck: String
    let editingMethod: String
    let provisionalInMs: Int
    let provisionalOutMs: Int
    let durationReason: String?
    let shotScale: [String]
    let composition: [String]
    let cameraAngle: [String]
    let narrativeIntent: String?
    let editorialFunction: String?
    let cinematicScores: [String: Double]?
    let cinematicPenalties: [String: Double]?
    let cinematicFinalScore: Double?
    let recommendationReason: String?
    let actualPrimarySubject: String?
    let secondarySubjects: [String]?
    let humanPresence: Bool?
    let humanSalience: String?
    let candidateShotRole: String?
    let gateStatus: String?
    let gatePenalty: Double?
    let gateReasonCodes: [String]?
    let gateReasons: [String]?
    let subjectMatchScore: Double?
    let shotRoleMatchScore: Double?
    let evidenceScore: Double?
    let truthfulnessScore: Double?
    let requiresSourceReview: Bool?
    let rankReason: String?
    let guideSourceTier: Int?
    let guideSourceLabel: String?
    let isPlaceholder: Bool?
    var id: String { candidateId }
}

struct EditorialGapStatus: Codable {
    let available: Bool
    let recommended: Bool
    let candidateSlotsConsumed: Int
    let reason: String
}

struct EditorialFavoriteSource: Decodable, Identifiable {
    let sourceContentId: String; let sourceFile: String; let note: String
    let previewPath: String?
    let previewTimeMs: Int?
    var id: String { sourceContentId }
}
struct EditorialFavoritesResponse: Decodable {
    let database: String; let sources: [EditorialFavoriteSource]
    let candidates: [EditorialCandidate]; let offset: Int; let nextOffset: Int?
    let totalFrames: Int; let message: String
}

struct EditorialSearchTarget: Equatable {
    let sessionId: String; let generation: UUID; let database: String; let beatId: String
}
struct EditorialSearchCandidateResponse: Decodable {
    let database: String; let visualUnitId: String; let candidate: EditorialCandidate
}

struct EditorialGateDiagnostic: Codable {
    let candidateId: String
    let sourceFile: String
    let gateStatus: String
    let reasonCodes: [String]
    let reasons: [String]
}

struct EditorialProjectGuidance: Codable {
    let matchConfidence: Double?
    let matchType: String?
    let excelRows: [Int]?
    let section: String?
    let guideNarration: String?
    let primaryShot: String?
    let visualDirection: String?
    let alternativeShot: String?
    let editingMethod: String?
    let notes: String?
    let guidanceStatus: String?
}

struct EditorialFallbackPlan: Codable {
    let contentRequirement: String?
    let aestheticRequirement: String?
    let editingResponsibility: String?
    let captureSuggestion: String?
}

struct EditorialGuideSummary: Codable {
    let sourceFile: String?
    let sheetName: String?
    let guideRowCount: Int?
    let matchedBeatCount: Int?
    let unmatchedBeatCount: Int?
}

struct EditorialBeat: Codable, Identifiable {
    let beatId: String
    let order: Int
    let text: String
    let purpose: String
    let estimatedNarrationMs: Int?
    let excludedVisualCount: Int?
    let requiredRoles: [String]
    let retrievalCandidateCount: Int
    let retrievalSourceCount: Int
    let narrativeIntent: String?
    let shotBrief: String?
    let visualTask: String?
    let expectedPrimarySubject: String?
    let preferredShotRoles: [String]?
    let visualizability: String?
    let contextBefore: [String]?
    let contextAfter: [String]?
    let visualTargetLabels: [String]?
    let projectEditorialGuidance: EditorialProjectGuidance?
    let guideSearchMessage: String?
    let guideMatchStatus: String?
    let fallbackPlan: EditorialFallbackPlan?
    let soundInstruction: Bool?
    let aRollPreference: String?
    let aRollOption: EditorialCandidate?
    let gapStatus: EditorialGapStatus?
    let gateDiagnostics: [EditorialGateDiagnostic]?
    var candidates: [EditorialCandidate]
    let reserveCandidates: [EditorialCandidate]?
    let candidateGroups: [String: [EditorialCandidate]]?
    var allCandidates: [EditorialCandidate] {
        var seen = Set<String>()
        return (candidates + (reserveCandidates ?? []) + (candidateGroups?["guide"] ?? []) + (candidateGroups?["system"] ?? [])).filter { seen.insert($0.candidateId).inserted }
    }
    var id: String { beatId }
}

struct EditorialBoardResponse: Codable {
    let status: String
    let track: String
    let database: String
    let databaseReadOnly: Bool
    let databaseWrite: Bool
    let modelRun: Bool
    let candidateCount: Int
    let ignoredChapterCards: [String]?
    let editorialGuideSummary: EditorialGuideSummary?
    let uiLabels: [String: String]?
    var beats: [EditorialBeat]
}

// A refresh needs sentence identity and saved guidance, not all candidate cards.
// Keep all sentences here: the backend validates order and derives full context.
struct EditorialSavedSelection: Identifiable {
    let beat: EditorialBeat
    let candidate: EditorialCandidate
    var id: String { beat.beatId + "::" + candidate.candidateId }
}

struct EditorialRefreshGuidance: Encodable {
    let beatId: String
    let text: String
    let projectEditorialGuidance: EditorialProjectGuidance?

    init(_ beat: EditorialBeat) {
        beatId = beat.beatId
        text = beat.text
        projectEditorialGuidance = beat.projectEditorialGuidance
    }
}

// Display-only, bounded in-memory cache. No candidate scores or user data files
// are cached here; changing libraries/label dictionaries invalidates every entry.
final class EditorialDisplayText {
    private var labels: [String: String] = [:]
    private var rules: [(String, NSRegularExpression, String)] = []
    private let cache = NSCache<NSString, NSString>()

    init() {
        cache.countLimit = 512
        cache.totalCostLimit = 1_048_576
    }

    func render(_ text: String, labels newLabels: [String: String]) -> String {
        if newLabels != labels {
            labels = newLabels
            cache.removeAllObjects()
            rules = labels.sorted { $0.key.count > $1.key.count }.compactMap { code, label in
                let pattern = "(?<![A-Za-z_])" + NSRegularExpression.escapedPattern(for: code) + "(?![A-Za-z_])"
                guard let regex = try? NSRegularExpression(pattern: pattern) else { return nil }
                return (code, regex, NSRegularExpression.escapedTemplate(for: label))
            }
        }
        if let existing = cache.object(forKey: text as NSString) { return existing as String }
        var result = text
        for (code, regex, replacement) in rules where result.contains(code) {
            result = regex.stringByReplacingMatches(in: result, range: NSRange(result.startIndex..., in: result), withTemplate: replacement)
        }
        let cost = text.utf8.count + result.utf8.count
        if cost <= 32_768 { cache.setObject(result as NSString, forKey: text as NSString, cost: cost) }
        return result
    }
}

struct EditorialSession: Codable {
    var formatVersion = "editorial_session_v1"
    var sessionId: String
    var savedAt: String
    var board: EditorialBoardResponse
    var script: String
    var generatedScript: String
    var guideFiles: [String]
    var generatedGuides: [String]
    var selectedFile: String
    var sourceLabel: String
    var activeBeat: Int
    var decisions: [String: String]
    var cutOverrides: [String: [Double]]
    var lockedCuts: Set<String>
    var skippedVisuals: [String: [EditorialCandidate]]
    var timelineName: String
    var frameRate: String
    var includeBackups: Bool
    var migrationNote: String?

    func validate() throws {
        func invalid(_ reason: String) -> NSError { NSError(domain: "EditorialSession", code: 1, userInfo: [NSLocalizedDescriptionKey: reason]) }
        guard formatVersion == "editorial_session_v1", UUID(uuidString: sessionId) != nil,
              !board.beats.isEmpty, board.beats.count <= 2000, board.beats.indices.contains(activeBeat),
              ["documentary", "short_video"].contains(board.track), board.databaseReadOnly, !board.databaseWrite else {
            throw invalid("工程格式或续做位置无效，现有工程未覆盖。")
        }
        var beatIds = Set<String>(), keys = Set<String>()
        for beat in board.beats {
            guard beatIds.insert(beat.beatId).inserted else { throw invalid("工程中句子编号重复。") }
            for candidate in beat.allCandidates + [beat.aRollOption].compactMap({ $0 }) {
                keys.insert(beat.beatId + "::" + candidate.candidateId)
            }
        }
        for (key, decision) in decisions {
            guard keys.contains(key), ["selected", "review", "rejected"].contains(decision) else { throw invalid("工程包含无法对应的人工选择，未丢弃这些记录。") }
        }
        for (key, range) in cutOverrides {
            guard keys.contains(key), range.count == 2, range.allSatisfy({ $0.isFinite && $0 >= 0 && $0 < 86_400_000 }), range[1] > range[0] else { throw invalid("工程剪点无效，未覆盖原存档。") }
        }
        guard lockedCuts.isSubset(of: Set(cutOverrides.keys)) else { throw invalid("锁定剪点缺少对应范围。") }
    }
}

enum EditorialSessionStore {
    static func encode(_ session: EditorialSession) throws -> Data {
        try session.validate()
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(session)
    }
    static func decode(_ data: Data) throws -> EditorialSession {
        guard data.count <= 128 * 1024 * 1024 else { throw NSError(domain: "EditorialSession", code: 2, userInfo: [NSLocalizedDescriptionKey:"工程文件超过128MB。"] ) }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let session = try decoder.decode(EditorialSession.self, from: data)
        try session.validate(); return session
    }
    static func save(_ session: EditorialSession, to url: URL) throws {
        let data = try encode(session)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
    }
}

// Only immutable value snapshots cross the queue. One serial writer prevents
// an older save from finishing after a newer save of the same project.
final class EditorialSessionWriter {
    typealias Completion = (Result<Void, Error>) -> Void
    private let queue = DispatchQueue(label: "editorial.project-writer", qos: .utility)
    private let lock = NSLock()
    private var pending: [URL: (EditorialSession, Bool, Completion)] = [:]
    private var failures: [URL: Error] = [:] // accessed on queue only

    func submit(_ session: EditorialSession, to url: URL, critical: Bool = true, completion: @escaping Completion) {
        lock.lock(); pending[url] = (session, critical, completion); lock.unlock()
        queue.async { self.drain() }
    }

    private func drain() {
        lock.lock(); let batch = pending; pending.removeAll(); lock.unlock()
        for (url, job) in batch {
            let result: Result<Void, Error>
            do { try EditorialSessionStore.save(job.0, to: url); failures.removeValue(forKey: url); result = .success(()) }
            catch { if job.1 { failures[url] = error }; result = .failure(error) }
            DispatchQueue.main.async { job.2(result) }
        }
    }

    // Used only at project-switch boundaries. Ordinary interactions never wait.
    func flush() throws {
        try queue.sync { drain(); if let error = failures.values.first { throw error } }
    }

    func finish(_ completion: @escaping Completion) {
        queue.async {
            self.drain()
            let result: Result<Void, Error> = self.failures.values.first.map { .failure($0) } ?? .success(())
            DispatchQueue.main.async { completion(result) }
        }
    }
}

// Reading/parsing a saved board can involve thousands of candidate records.
// Never do it on the UI thread; only publish the finished value on main.
enum EditorialProjectReader {
    enum Contents { case session(EditorialSession), legacy(URL) }
    private struct Header: Decodable { var formatVersion: String?; var contractVersion: String? }
    private static let queue = DispatchQueue(label: "editorial.project-reader", qos: .userInitiated)

    static func read(url: URL? = nil, directory: URL? = nil, completion: @escaping (Result<Contents, Error>) -> Void) {
        queue.async {
            let result = Result<Contents, Error> {
                let target: URL
                if let url { target = url }
                else if let directory {
                    let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.contentModificationDateKey])
                        .filter { $0.pathExtension == "json" }
                        .map { ($0, (try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast) }
                    guard let latest = files.max(by: { $0.1 < $1.1 })?.0 else {
                        throw NSError(domain: "EditorialSession", code: 6, userInfo: [NSLocalizedDescriptionKey: "还没有自动存档，请先打开工程文件。"])
                    }
                    target = latest
                } else { throw CocoaError(.fileNoSuchFile) }
                guard (try target.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0) <= 128 * 1024 * 1024 else {
                    throw NSError(domain: "EditorialSession", code: 4, userInfo: [NSLocalizedDescriptionKey: "工程文件超过128MB。"])
                }
                let data = try Data(contentsOf: target)
                guard data.count <= 128 * 1024 * 1024 else { throw CocoaError(.fileReadTooLarge) }
                let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
                let header = try decoder.decode(Header.self, from: data)
                if header.formatVersion != nil { return .session(try EditorialSessionStore.decode(data)) }
                if header.contractVersion == "editorial_manifest_v125_v1" { return .legacy(target) }
                throw NSError(domain: "EditorialSession", code: 7, userInfo: [NSLocalizedDescriptionKey: "不是选片工程或受支持的旧版剪辑清单。"])
            }
            DispatchQueue.main.async { completion(result) }
        }
    }

    static func decode(_ data: Data, completion: @escaping (Result<EditorialSession, Error>) -> Void) {
        queue.async {
            let result = Result { try EditorialSessionStore.decode(data) }
            DispatchQueue.main.async { completion(result) }
        }
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

enum ArchivePage: String, CaseIterable, Identifiable {
    case newTask = "新建任务", running = "运行状态", history = "任务历史"
    case search = "搜索素材", favorites = "我的收藏"
    case editorial = "文稿选片（实验）", reference = "剪辑参考"
    case duplicates = "重复素材", special = "特殊素材", settings = "设置"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .newTask: return "plus.circle"
        case .running: return "play.circle"
        case .history: return "clock"
        case .search: return "magnifyingglass"
        case .favorites: return "heart.fill"
        case .editorial: return "rectangle.and.pencil.and.ellipsis"
        case .reference: return "book.closed"
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
    @Published var editorialScript = ""
    @Published var editorialTrack = "documentary"
    @Published var editorialBoard: EditorialBoardResponse? { didSet { invalidateEditorialViewCache() } }
    @Published var editorialActiveBeat = 0 { didSet { if oldValue != editorialActiveBeat { autosaveEditorialSession() } } }
    @Published var editorialReviewReturnBeatId: String?
    @Published var editorialLoading = false
    @Published var editorialStatus = "输入或载入文稿后生成候选。"
    @Published var editorialSourceLabel = "直接输入"
    @Published var editorialFolderFiles: [URL] = []
    @Published var editorialSelectedFile = ""
    @Published var editorialGuideFile = ""
    @Published var editorialGuideFiles: [String] = []
    @Published var editorialGuideLabel = "未载入逐句剪辑指导（可选）"
    @Published var editorialChapterCards: [String] = []
    @Published var editorialDecisions: [String: String] = [:] { didSet { if oldValue != editorialDecisions { invalidateEditorialViewCache(); autosaveEditorialSession() } } }
    @Published var editorialTimelineName = "文稿候选粗剪" { didSet { if oldValue != editorialTimelineName { autosaveEditorialSession() } } }
    @Published var editorialFrameRate = "30000/1001" { didSet { if oldValue != editorialFrameRate { autosaveEditorialSession() } } }
    @Published var editorialExportStatus = ""
    @Published var editorialPreviewStatus: [String: String] = [:]
    @Published var editorialPreviewPending: Set<String> = []
    @Published var editorialDecisionStatus = "入选与备选都会保留在右侧；再次点击同一按钮可取消。"
    @Published var editorialIncludeBackups = true { didSet { if oldValue != editorialIncludeBackups { autosaveEditorialSession() } } }
    @Published var editorialRefreshStatus = ""
    @Published var editorialCutOverrides: [String: [Double]] = [:] { didSet { if oldValue != editorialCutOverrides { autosaveEditorialSession() } } }
    @Published var editorialLockedCuts: Set<String> = [] { didSet { if oldValue != editorialLockedCuts { autosaveEditorialSession() } } }
    @Published var editorialSkippedVisuals: [String: [EditorialCandidate]] = [:] { didSet { invalidateEditorialViewCache(); autosaveEditorialSession() } }
    private var editorialCandidateCache: [String: [EditorialCandidate]] = [:]
    private var editorialSavedSelectionCache: [EditorialSavedSelection]?
    @Published var editorialSessionStatus = "支持保存未完成工程；不需要先选完，也不需要连接原素材盘。"
    private var editorialSessionId = UUID().uuidString
    private var editorialSessionRestoring = false
    private let editorialWriter = EditorialSessionWriter()
    private let editorialDisplayText = EditorialDisplayText()
    private var editorialSaveRevision = 0
    @Published var editorialFavoritesPresented = false
    @Published var editorialFavoritesLoading = false
    @Published var editorialFavorites: EditorialFavoritesResponse?
    @Published var editorialFavoriteSourceId = ""
    @Published var editorialFavoriteStatus = ""
    @Published var editorialSearchTarget: EditorialSearchTarget?
    @Published var editorialSearchPending = false
    @Published var editorialSearchMessages: [String: String] = [:]
    private var editorialFavoriteRequest = UUID()
    private var editorialFavoriteBeatId = ""
    private var editorialRefreshInProgress = false
    private var editorialRefreshWanted = false
    private var editorialGeneration = UUID()
    private var editorialGeneratedScript = ""
    private var editorialGeneratedGuides: [String] = []
    private var editorialGeneratedTrack = "documentary"
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
                    self.openMainPage(.search)
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
        editorialSearchTarget = nil; editorialSearchMessages = [:]
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

    func editorialKey(_ beatId: String, _ candidateId: String) -> String {
        beatId + "::" + candidateId
    }

    private func sameEditorialVisual(_ left: EditorialCandidate, _ right: EditorialCandidate) -> Bool {
        guard !left.sourceContentId.isEmpty, !right.sourceContentId.isEmpty else { return false }
        guard left.sourceContentId == right.sourceContentId else { return false }
        if left.mediaType == "image" || right.mediaType == "image" { return true }
        let overlaps = max(left.startMs, right.startMs) < min(left.endMs, right.endMs)
        if overlaps { return true }
        if let leftAnchor = left.anchorTimeMs, let rightAnchor = right.anchorTimeMs {
            return abs(leftAnchor - rightAnchor) <= 5_000
        }
        return false
    }

    func editorialText(_ text: String) -> String {
        editorialDisplayText.render(text, labels: editorialBoard?.uiLabels ?? [:])
    }

    func editorialCandidates(for beatIndex: Int, channel: String = "system") -> [EditorialCandidate] {
        guard let board = editorialBoard, board.beats.indices.contains(beatIndex) else { return [] }
        let cacheKey = "\(beatIndex)::\(channel)"
        if let cached = editorialCandidateCache[cacheKey] { return cached }
        let result = computeEditorialCandidates(for: beatIndex, channel: channel)
        editorialCandidateCache[cacheKey] = result
        return result
    }

    private func computeEditorialCandidates(for beatIndex: Int, channel: String) -> [EditorialCandidate] {
        guard let board = editorialBoard, board.beats.indices.contains(beatIndex) else { return [] }
        let beat = board.beats[beatIndex]
        let rows = beat.candidateGroups?[channel] ?? (channel == "guide" ? [] : beat.allCandidates)
        let skipped = editorialSkippedVisuals[beat.beatId] ?? []
        let guideVisible = channel == "system" && beat.candidateGroups != nil ? editorialCandidates(for: beatIndex, channel: "guide") : []
        let reserved = board.beats.enumerated().filter { $0.offset != beatIndex }.flatMap { _, beat in
            beat.allCandidates.filter {
                ["selected", "review"].contains(editorialDecisions[editorialKey(beat.beatId, $0.candidateId)] ?? "")
            }
        }
        let previous = board.beats.prefix(beatIndex).flatMap { beat in
            beat.allCandidates.filter {
                editorialDecisions[editorialKey(beat.beatId, $0.candidateId)] == "selected"
            }
        }.last
        let next = board.beats.dropFirst(beatIndex + 1).flatMap { beat in
            beat.allCandidates.filter { editorialDecision(beat.beatId, $0.candidateId) == "selected" }
        }.first
        var result: [EditorialCandidate] = []
        for candidate in rows where candidate.isPlaceholder != true {
            if editorialDecision(beat.beatId, candidate.candidateId) == "rejected" { continue }
            if skipped.contains(where: { sameEditorialVisual($0, candidate) }) { continue }
            if guideVisible.contains(where: { sameEditorialVisual($0, candidate) }) { continue }
            if reserved.contains(where: { sameEditorialVisual($0, candidate) }) { continue }
            if result.contains(where: { sameEditorialVisual($0, candidate) }) { continue }
            result.append(candidate)
        }
        if beat.candidateGroups != nil {
            // Keep the two retrieval routes independent, with cheap local
            // sequence adjustment when human choices change (no DB requery).
            let ranks = Dictionary(uniqueKeysWithValues: result.enumerated().map { ($0.element.candidateId, Double($0.offset)) })
            func sequenceOrder(_ candidate: EditorialCandidate) -> Double {
                var order = ranks[candidate.candidateId] ?? 0
                for neighbor in [previous, next].compactMap({ $0 }) {
                    if !candidate.sourceContentId.isEmpty && candidate.sourceContentId == neighbor.sourceContentId { order += 2 }
                    if !Set(candidate.shotScale).isDisjoint(with: Set(neighbor.shotScale)) { order += 0.6 }
                    if !Set(candidate.composition).isDisjoint(with: Set(neighbor.composition)) { order += 0.4 }
                    if candidate.editorialFunction == neighbor.editorialFunction { order += 0.4 }
                }
                return order
            }
            result.sort {
                if ($0.gateStatus == "SOFT_GATE") != ($1.gateStatus == "SOFT_GATE") { return $0.gateStatus != "SOFT_GATE" }
                if channel == "guide" && ($0.guideSourceTier ?? 4) != ($1.guideSourceTier ?? 4) { return ($0.guideSourceTier ?? 4) < ($1.guideSourceTier ?? 4) }
                let left = sequenceOrder($0), right = sequenceOrder($1)
                return left == right ? (ranks[$0.candidateId] ?? 0) < (ranks[$1.candidateId] ?? 0) : left < right
            }
            return Array(result.prefix(3))
        }
        result.sort { left, right in
            let leftGate = left.gateStatus == "SOFT_GATE" ? 1 : 0
            let rightGate = right.gateStatus == "SOFT_GATE" ? 1 : 0
            if leftGate != rightGate { return leftGate < rightGate }
            if (left.guideSourceTier ?? 4) != (right.guideSourceTier ?? 4) {
                return (left.guideSourceTier ?? 4) < (right.guideSourceTier ?? 4)
            }
            func adjusted(_ candidate: EditorialCandidate) -> Double {
                var score = candidate.cinematicFinalScore ?? 0
                for neighbor in [previous, next].compactMap({ $0 }) {
                    if !candidate.sourceContentId.isEmpty && candidate.sourceContentId == neighbor.sourceContentId { score -= 10 }
                    if !Set(candidate.shotScale).isDisjoint(with: Set(neighbor.shotScale)) { score -= 3 }
                    if !Set(candidate.composition).isDisjoint(with: Set(neighbor.composition)) { score -= 2 }
                    if candidate.editorialFunction == neighbor.editorialFunction { score -= 2 }
                }
                return score
            }
            let leftScore = adjusted(left), rightScore = adjusted(right)
            return leftScore == rightScore ? left.shortlistRank < right.shortlistRank : leftScore > rightScore
        }
        return Array(result.prefix(3))
    }

    func editorialDecision(_ beatId: String, _ candidateId: String) -> String {
        editorialDecisions[editorialKey(beatId, candidateId)] ?? ""
    }

    func editorialSavedCandidates(for beat: EditorialBeat) -> [EditorialCandidate] {
        (beat.allCandidates + [beat.aRollOption].compactMap({ $0 })).filter {
            ["selected", "review"].contains(editorialDecision(beat.beatId, $0.candidateId))
        }
    }

    private func invalidateEditorialViewCache() {
        editorialCandidateCache.removeAll(keepingCapacity: true)
        editorialSavedSelectionCache = nil
    }

    func editorialSavedSelections() -> [EditorialSavedSelection] {
        if let cached = editorialSavedSelectionCache { return cached }
        let rows = (editorialBoard?.beats ?? []).flatMap { beat in
            editorialSavedCandidates(for: beat).map { EditorialSavedSelection(beat: beat, candidate: $0) }
        }
        editorialSavedSelectionCache = rows
        return rows
    }

    func setEditorialDecision(beat: EditorialBeat, candidate: EditorialCandidate, decision: String) {
        let key = editorialKey(beat.beatId, candidate.candidateId)
        var choices = editorialDecisions
        if choices[key] == decision {
            choices.removeValue(forKey: key)
            editorialDecisions = choices
            editorialDecisionStatus = "已取消第 \(beat.order) 句的\(decision == "selected" ? "入选" : decision == "review" ? "备选" : "排除")。"
            return
        }
        if decision == "selected" || decision == "review" {
            for row in beat.allCandidates + [beat.aRollOption].compactMap({ $0 }) {
                let otherKey = editorialKey(beat.beatId, row.candidateId)
                if choices[otherKey] == decision {
                    choices.removeValue(forKey: otherKey)
                }
            }
            if let board = editorialBoard {
                for otherBeat in board.beats where otherBeat.beatId != beat.beatId {
                    for otherCandidate in otherBeat.allCandidates where sameEditorialVisual(candidate, otherCandidate) {
                        choices.removeValue(
                            forKey: editorialKey(otherBeat.beatId, otherCandidate.candidateId)
                        )
                    }
                }
            }
        }
        choices[key] = decision
        editorialDecisions = choices
        editorialDecisionStatus = "第 \(beat.order) 句已\(decision == "selected" ? "加入主选" : decision == "review" ? "加入备选" : "排除")；再次点击可取消。保存结果请看顶部工程状态。"
    }

    func editorialCut(_ beat: EditorialBeat, _ candidate: EditorialCandidate) -> [Double] {
        editorialCutOverrides[editorialKey(beat.beatId, candidate.candidateId)] ??
            [Double(candidate.provisionalInMs) / 1000, Double(candidate.provisionalOutMs) / 1000]
    }

    func editorialCutBinding(_ beat: EditorialBeat, _ candidate: EditorialCandidate, _ endpoint: Int) -> Binding<Double> {
        Binding(get: { self.editorialCut(beat, candidate)[endpoint] }, set: { value in
            guard !self.editorialLockedCuts.contains(self.editorialKey(beat.beatId, candidate.candidateId)) else { return }
            var range = self.editorialCut(beat, candidate)
            guard value.isFinite, range[endpoint] != value else { return }
            range[endpoint] = value
            self.editorialCutOverrides[self.editorialKey(beat.beatId, candidate.candidateId)] = range
        })
    }

    func activateEditorialBeat(_ index: Int) {
        guard let board = editorialBoard, board.beats.indices.contains(index), !editorialLoading else { return }
        guard editorialActiveBeat != index else { return }
        editorialActiveBeat = index
        editorialRefreshStatus = "已显示本句缓存候选；切换句子不会重新查询。可点“换一批”继续筛，或主动重新核对本句。"
    }

    func reviewEditorialBeat(_ beatId: String) {
        guard let board = editorialBoard, !editorialLoading,
              board.beats.indices.contains(editorialActiveBeat),
              let target = board.beats.firstIndex(where: { $0.beatId == beatId }) else { return }
        if target != editorialActiveBeat && editorialReviewReturnBeatId == nil {
            editorialReviewReturnBeatId = board.beats[editorialActiveBeat].beatId
        }
        activateEditorialBeat(target)
    }

    var editorialReviewReturnIndex: Int? {
        guard let id = editorialReviewReturnBeatId else { return nil }
        return editorialBoard?.beats.firstIndex(where: { $0.beatId == id })
    }

    func finishEditorialReview() {
        guard !editorialLoading else { return }
        if let target = editorialReviewReturnIndex { activateEditorialBeat(target) }
        editorialReviewReturnBeatId = nil
    }

    func openEditorialFavorites(sourceId: String = "") {
        guard let board = editorialBoard, board.beats.indices.contains(editorialActiveBeat), !editorialLoading else { return }
        editorialFavoriteBeatId = board.beats[editorialActiveBeat].beatId
        editorialFavorites = nil; editorialFavoriteSourceId = ""
        editorialFavoritesPresented = true
        loadEditorialFavorites(sourceId: sourceId)
    }

    func loadEditorialFavorites(sourceId: String = "", offset: Int = 0) {
        guard let board = editorialBoard else { return }
        let request = UUID(); editorialFavoriteRequest = request
        let generation = editorialGeneration
        editorialFavoritesLoading = true; editorialFavoriteSourceId = sourceId
        editorialFavoriteStatus = "只读加载当前素材库的收藏与已有画面…"
        runHelper(["editorial-favorites", "--board-database", board.database,
                   "--source-content-id", sourceId, "--offset", String(offset)]) { data, error in
            guard self.editorialFavoriteRequest == request, self.editorialGeneration == generation else { return }
            self.editorialFavoritesLoading = false
            do {
                if let error { throw NSError(domain: "EditorialFavorites", code: 1, userInfo: [NSLocalizedDescriptionKey:error]) }
                guard let data else { return }
                let response = try self.decoder().decode(EditorialFavoritesResponse.self, from: data)
                guard response.database == board.database else { throw NSError(domain: "EditorialFavorites", code: 2, userInfo: [NSLocalizedDescriptionKey:"收藏所属素材库不一致"]) }
                self.editorialFavorites = response
                self.editorialFavoriteStatus = response.message
            } catch { self.editorialFavorites = nil; self.editorialFavoriteStatus = "读取收藏失败：\(error.localizedDescription)；原选择不变。" }
        }
    }

    func editorialFavoriteConflict(_ candidate: EditorialCandidate) -> String? {
        guard let board = editorialBoard else { return "请先打开选片工程" }
        for beat in board.beats where beat.beatId != editorialFavoriteBeatId {
            if editorialSavedCandidates(for: beat).contains(where: { sameEditorialVisual($0, candidate) }) {
                return "第 \(beat.order) 句已占用这一画面；先取消旧选择，避免无意抢走。"
            }
        }
        return nil
    }

    func chooseEditorialFavorite(_ candidate: EditorialCandidate, decision: String) {
        guard !editorialFavoritesLoading, var board = editorialBoard,
              board.beats.indices.contains(editorialActiveBeat), board.beats[editorialActiveBeat].beatId == editorialFavoriteBeatId else { return }
        if let conflict = editorialFavoriteConflict(candidate) { editorialFavoriteStatus = conflict; return }
        let index = editorialActiveBeat
        board.beats[index].candidates.removeAll { $0.candidateId == candidate.candidateId }
        board.beats[index].candidates.insert(candidate, at: 0)
        editorialBoard = board
        setEditorialDecision(beat: board.beats[index], candidate: candidate, decision: decision)
        editorialFavoriteStatus = "第 \(index + 1) 句：\(editorialDecision(board.beats[index].beatId, candidate.candidateId).isEmpty ? "已取消" : "已加入候选箱")。剪点可在候选箱播放、调整并锁定。"
    }

    // Search is a manual fallback, not another rerank pass. Keep the destination
    // pinned while the user plays clips and browses all frames of a source.
    var editorialSearchBeat: EditorialBeat? {
        guard let target = editorialSearchTarget, let board = editorialBoard,
              target.sessionId == editorialSessionId, target.generation == editorialGeneration,
              target.database == board.database,
              let current = snapshot?.searchRuntime.databasePreflight?.databasePath,
              URL(fileURLWithPath: current).standardizedFileURL == URL(fileURLWithPath: target.database).standardizedFileURL
        else { return nil }
        return board.beats.first(where: { $0.beatId == target.beatId })
    }

    func bindEditorialSearchTarget(_ beatId: String? = nil) {
        guard !editorialLoading, !editorialSearchPending, let board = editorialBoard,
              board.beats.indices.contains(editorialActiveBeat) else { return }
        let id = beatId ?? board.beats[editorialActiveBeat].beatId
        guard board.beats.contains(where: { $0.beatId == id }) else { return }
        editorialSearchTarget = EditorialSearchTarget(sessionId: editorialSessionId,
            generation: editorialGeneration, database: board.database, beatId: id)
        editorialSearchMessages = [:]
    }

    // Main navigation is ordinary library use, even with a saved editorial
    // project open. Only startEditorialSearch opts into sentence-bound search.
    func openMainPage(_ destination: ArchivePage) {
        if editorialSearchTarget != nil && searchStatus.hasPrefix("已带入第 ") {
            searchStatus = "搜索条件已保留；可修改关键词后搜索。"
        }
        editorialSearchTarget = nil
        editorialSearchMessages = [:]
        page = destination
    }

    func startEditorialSearch() {
        guard !searching, !editorialLoading, !editorialSearchPending else { return }
        bindEditorialSearchTarget()
        guard let beat = editorialSearchBeat else {
            editorialDecisionStatus = "请先连接选片工程对应的素材库，再从搜索补选。"; return
        }
        let direction = beat.projectEditorialGuidance?.visualDirection?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        query = direction.isEmpty ? beat.text : direction
        // Guide dates describe folder/shooting dates, not filesystem mtime.
        // Keep them visible as reference; never silently turn them into mtime filters.
        searchPathPrefix = ""; searchDateFrom = ""; searchDateTo = ""
        searchRequireOCR = false; searchRequirePerson = false; mediaType = "全部"
        activePersonClusterId = ""; activePersonSourceId = ""; activeSourceContentId = ""; selectedPersonClusterId = ""
        searchResults = []; bufferedSearchResults = []; searchCoverage = nil
        searchTotalCount = 0; nextSearchOffset = nil; serverNextSearchOffset = nil; lastSearchSignature = ""
        selectedExportResults = [:]; clearSearchNavigation()
        searchStatus = "已带入第 \(beat.order) 句的画面要求。可改关键词或路径，再点搜索；尚未开始查询。"
        searchDiagnostic = ""
        page = .search
    }

    func returnFromEditorialSearch() {
        guard let beat = editorialSearchBeat, !editorialLoading,
              let index = editorialBoard?.beats.firstIndex(where: { $0.beatId == beat.beatId }) else { return }
        activateEditorialBeat(index)
        openMainPage(.editorial)
    }

    func addSearchResultToEditorial(_ result: SearchResult, decision: String) {
        guard ["selected", "review"].contains(decision), !editorialSearchPending, !editorialLoading,
              let target = editorialSearchTarget, editorialSearchBeat != nil else { return }
        let key = result.exportSelectionId
        guard let source = result.sourceContentId, !source.isEmpty,
              let visual = result.visualUnitId, !visual.isEmpty, result.mediaType != "audio" else {
            editorialSearchMessages[key] = "请浏览该视频全部画面，选择一个具体画面；音频条目不能直接作为视频补选。"; return
        }
        editorialSearchPending = true
        editorialSearchMessages[key] = "正在核对这一帧的原文件与选片记录…"
        runHelper(["editorial-search-candidate", "--board-database", target.database,
                   "--source-content-id", source, "--visual-unit-id", visual]) { data, error in
            self.editorialSearchPending = false
            guard self.editorialSearchTarget == target, let beat = self.editorialSearchBeat,
                  !self.editorialLoading else {
                self.editorialSearchMessages[key] = "工程、素材库或补选目标已变化，本次未加入。"; return
            }
            do {
                if let error { throw NSError(domain: "EditorialSearch", code: 1, userInfo: [NSLocalizedDescriptionKey:error]) }
                guard let data else { throw NSError(domain: "EditorialSearch", code: 2, userInfo: [NSLocalizedDescriptionKey:"没有收到核对结果"]) }
                let response = try self.decoder().decode(EditorialSearchCandidateResponse.self, from: data)
                guard URL(fileURLWithPath: response.database).standardizedFileURL == URL(fileURLWithPath: target.database).standardizedFileURL,
                      response.visualUnitId == visual, response.candidate.sourceContentId == source else {
                    throw NSError(domain: "EditorialSearch", code: 3, userInfo: [NSLocalizedDescriptionKey:"画面所属素材库或编号不一致"])
                }
                self.commitEditorialSearchCandidate(response.candidate, beat: beat, decision: decision, messageKey: key)
            } catch { self.editorialSearchMessages[key] = "未加入：\(error.localizedDescription)；原选择不变。" }
        }
    }

    func commitEditorialSearchCandidate(_ incoming: EditorialCandidate, beat: EditorialBeat, decision: String, messageKey: String) {
        guard var board = editorialBoard, let index = board.beats.firstIndex(where: { $0.beatId == beat.beatId }) else { return }
        for other in board.beats where other.beatId != beat.beatId {
            if editorialSavedCandidates(for: other).contains(where: { sameEditorialVisual($0, incoming) }) {
                editorialSearchMessages[messageKey] = "未加入：第 \(other.order) 句已使用这一画面。先到原句取消，避免抢走已有选择。"; return
            }
        }
        // Preserve any prior cut/lock and candidate metadata on this exact ID.
        let candidate = beat.allCandidates.first(where: { $0.candidateId == incoming.candidateId }) ?? incoming
        if editorialDecision(beat.beatId, candidate.candidateId) == decision {
            editorialSearchMessages[messageKey] = "第 \(beat.order) 句已经\(decision == "selected" ? "入选" : "备选")此帧，无需重复加入。可回候选箱修改或取消。"; return
        }
        if let existing = editorialSavedCandidates(for: beat).first(where: { editorialDecision(beat.beatId, $0.candidateId) == decision }) {
            let alert = NSAlert(); alert.messageText = "替换第 \(beat.order) 句的\(decision == "selected" ? "主选" : "备选")？"
            alert.informativeText = "原选择：\(existing.sourceFile)\n新选择：\(candidate.sourceFile)\n只替换本句这一项，不改其他句和已保存剪点。"
            alert.addButton(withTitle: "确认替换"); alert.addButton(withTitle: "取消")
            guard alert.runModal() == .alertFirstButtonReturn else { editorialSearchMessages[messageKey] = "已取消替换，原选择不变。"; return }
        }
        board.beats[index].candidates.removeAll { $0.candidateId == candidate.candidateId }
        board.beats[index].candidates.insert(candidate, at: 0)
        editorialBoard = board
        setEditorialDecision(beat: board.beats[index], candidate: candidate, decision: decision)
        let cut = editorialCut(board.beats[index], candidate)
        editorialSearchMessages[messageKey] = String(format: "已加入第 %d 句%@ · 暂用 %.2f → %.2f 秒。回选片可播放、调整并锁定；保存结果见下方工程状态。", beat.order, decision == "selected" ? "主选" : "备选", cut[0], cut[1])
    }

    private var editorialSessionDirectory: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("LocalMediaOrganizer/EditorialProjects", isDirectory: true)
    }

    private func captureEditorialSession() -> EditorialSession? {
        guard let board = editorialBoard, !editorialLoading, !editorialSessionRestoring else { return nil }
        let keys = Set(board.beats.flatMap { beat in
            (beat.allCandidates + [beat.aRollOption].compactMap({ $0 })).map { editorialKey(beat.beatId, $0.candidateId) }
        })
        return EditorialSession(sessionId: editorialSessionId, savedAt: ISO8601DateFormatter().string(from: Date()),
            board: board, script: editorialScript, generatedScript: editorialGeneratedScript,
            guideFiles: editorialGuideFiles, generatedGuides: editorialGeneratedGuides,
            selectedFile: editorialSelectedFile, sourceLabel: editorialSourceLabel, activeBeat: editorialActiveBeat,
            decisions: editorialDecisions, cutOverrides: editorialCutOverrides.filter { keys.contains($0.key) },
            lockedCuts: editorialLockedCuts.intersection(keys), skippedVisuals: editorialSkippedVisuals,
            timelineName: editorialTimelineName, frameRate: editorialFrameRate, includeBackups: editorialIncludeBackups)
    }

    func autosaveEditorialSession() {
        guard let session = captureEditorialSession() else { return }
        editorialSaveRevision += 1
        let revision = editorialSaveRevision
        editorialSessionStatus = "正在后台保存第 \(session.activeBeat + 1) 句的进度…请等到“已保存”再强制退出。"
        let url = editorialSessionDirectory.appendingPathComponent(session.sessionId + ".json")
        editorialWriter.submit(session, to: url) { [weak self] result in
            guard let self, self.editorialSessionId == session.sessionId, self.editorialSaveRevision == revision else { return }
            switch result {
            case .success: self.editorialSessionStatus = "已自动保存 · 第 \(session.activeBeat + 1) 句 · \(Date().formatted(date: .omitted, time: .standard))。关闭后可继续最近工程。"
            case .failure(let error): self.editorialSessionStatus = "工程未保存：\(error.localizedDescription)；请勿关闭，先保存工程副本。"
            }
        }
    }

    func finishEditorialSaves(_ completion: @escaping EditorialSessionWriter.Completion) {
        autosaveEditorialSession()
        editorialWriter.finish(completion)
    }

    func saveEditorialProjectCopy() {
        guard let session = captureEditorialSession() else { editorialSessionStatus = "先生成或打开选片工程。"; return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.json]
        panel.nameFieldStringValue = "\(editorialTimelineName)_未完成工程.json"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        editorialSessionStatus = "正在保存工程副本…"
        editorialWriter.submit(session, to: url, critical: false) { [weak self] result in
            guard let self, self.editorialSessionId == session.sessionId else { return }
            switch result {
            case .success: self.editorialSessionStatus = "工程副本已保存：\(url.lastPathComponent)。可在新版打开，不必选完。"
            case .failure(let error): self.editorialSessionStatus = "工程副本保存失败：\(error.localizedDescription)"
            }
        }
    }

    private func applyEditorialSession(_ session: EditorialSession, continuing: Bool = false) throws {
        try session.validate()
        if let current = snapshot?.searchRuntime.databasePreflight?.databasePath,
           URL(fileURLWithPath: current).standardizedFileURL != URL(fileURLWithPath: session.board.database).standardizedFileURL {
            throw NSError(domain: "EditorialSession", code: 3, userInfo: [NSLocalizedDescriptionKey:"工程属于另一素材库，请先在任务历史连接对应素材库。原选择未覆盖。"])
        }
        if let current = captureEditorialSession() {
            try editorialWriter.flush()
            try EditorialSessionStore.save(current, to: editorialSessionDirectory.appendingPathComponent(current.sessionId + ".json"))
        }
        editorialSessionRestoring = true
        editorialReviewReturnBeatId = nil
        editorialGeneration = UUID()
        editorialSessionId = continuing ? session.sessionId : UUID().uuidString
        editorialBoard = session.board
        editorialScript = session.script; editorialGeneratedScript = session.generatedScript
        editorialTrack = session.board.track; editorialGeneratedTrack = session.board.track
        editorialGuideFiles = session.guideFiles; editorialGeneratedGuides = session.generatedGuides
        editorialGuideFile = session.guideFiles.first ?? ""
        editorialGuideLabel = session.guideFiles.isEmpty ? "使用工程内保存的逐句指导；不需要重新上传Excel" : "已恢复 \(session.guideFiles.count) 份指导文件引用及内嵌指导"
        editorialSelectedFile = session.selectedFile; editorialSourceLabel = session.sourceLabel
        editorialActiveBeat = session.activeBeat; editorialDecisions = session.decisions
        editorialCutOverrides = session.cutOverrides; editorialLockedCuts = session.lockedCuts
        editorialSkippedVisuals = session.skippedVisuals
        editorialTimelineName = session.timelineName; editorialFrameRate = session.frameRate
        editorialIncludeBackups = session.includeBackups
        editorialPreviewStatus = [:]; editorialPreviewPending = []
        editorialRefreshWanted = false
        editorialStatus = "已恢复 \(session.board.beats.count) 句，不重新选片、不改已选镜头。可继续下一句，或主动核对当前句的新候选。"
        editorialDecisionStatus = "已恢复主选 \(session.decisions.values.filter { $0 == "selected" }.count) 个、备选 \(session.decisions.values.filter { $0 == "review" }.count) 个；剪点和排除记录保留。"
        editorialRefreshStatus = session.migrationNote ?? "已回到保存时的第 \(session.activeBeat + 1) 句。"
        editorialSessionRestoring = false
        page = .editorial
        autosaveEditorialSession()
    }

    func openEditorialProject() {
        guard !editorialLoading else { return }
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.json]
        panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        readEditorialProject(url: url, continuing: false)
    }

    func continueRecentEditorialProject() {
        guard !editorialLoading else { return }
        readEditorialProject(url: nil, continuing: true)
    }

    private func readEditorialProject(url: URL?, continuing: Bool) {
        let generation = editorialGeneration
        let choices = editorialDecisions, cuts = editorialCutOverrides, locks = editorialLockedCuts
        let script = editorialScript
        editorialLoading = true
        editorialSessionStatus = "正在后台读取工程，已选镜头保持不变…"
        func finish(_ result: Result<EditorialSession, Error>) {
            guard self.editorialGeneration == generation else {
                self.editorialLoading = false
                self.editorialSessionStatus = "当前工程已切换，忽略旧的读取结果。"; return
            }
            self.editorialLoading = false
            guard self.editorialDecisions == choices, self.editorialCutOverrides == cuts,
                  self.editorialLockedCuts == locks, self.editorialScript == script else {
                self.autosaveEditorialSession()
                self.editorialSessionStatus = "读取期间有新的编辑，已保留当前选择；请再次打开工程。"
                return
            }
            do { try self.applyEditorialSession(result.get(), continuing: continuing) }
            catch { self.editorialSessionStatus = "工程恢复失败：\(error.localizedDescription)；原文件和原选择未覆盖。" }
        }
        EditorialProjectReader.read(url: url, directory: editorialSessionDirectory) { result in
            guard self.editorialGeneration == generation else {
                finish(.failure(CocoaError(.userCancelled))); return
            }
            switch result {
            case .failure(let error): finish(.failure(error))
            case .success(.session(let session)): finish(.success(session))
            case .success(.legacy(let file)):
                self.editorialSessionStatus = "正在恢复旧版清单；只读核对素材编号，不重新排序…"
                self.runHelper(["editorial-import-manifest", "--input", file.path]) { data, error in
                    if let error {
                        finish(.failure(NSError(domain: "EditorialSession", code: 5, userInfo: [NSLocalizedDescriptionKey: error])))
                    } else if let data { EditorialProjectReader.decode(data, completion: finish) }
                    else { finish(.failure(CocoaError(.fileReadCorruptFile))) }
                }
            }
        }
    }

    func nextEditorialBatch(_ channel: String) {
        guard let board = editorialBoard, board.beats.indices.contains(editorialActiveBeat) else { return }
        let beat = board.beats[editorialActiveBeat]
        let batch = editorialCandidates(for: editorialActiveBeat, channel: channel).filter {
            !["selected", "review"].contains(editorialDecision(beat.beatId, $0.candidateId))
        }
        editorialSkippedVisuals[beat.beatId, default: []] += batch
        editorialRefreshStatus = "本句已跳过 \(editorialSkippedVisuals[beat.beatId]?.count ?? 0) 个画面，本次选片中不再推荐这些窗口；人工入选/备选保持不变。"
        if editorialCandidates(for: editorialActiveBeat, channel: channel).count < 3 { refreshEditorialBeat() }
    }

    func refreshEditorialBeat() {
        guard let board = editorialBoard, board.beats.indices.contains(editorialActiveBeat), !editorialLoading else { return }
        editorialRefreshWanted = true
        guard !editorialRefreshInProgress else { return }
        editorialRefreshWanted = false
        editorialRefreshInProgress = true
        let index = editorialActiveBeat
        let beatId = board.beats[index].beatId
        let generation = editorialGeneration
        var reserved: [[String: Any]] = board.beats.filter { $0.beatId != beatId }.flatMap { beat in
            editorialSavedCandidates(for: beat).filter { $0.isPlaceholder != true }.map { candidate in
                ["source_content_id": candidate.sourceContentId, "media_type": candidate.mediaType,
                 "start_ms": candidate.startMs, "end_ms": candidate.endMs,
                 "anchor_time_ms": candidate.anchorTimeMs as Any? ?? NSNull()] as [String: Any]
            }
        }
        let skipped = (editorialSkippedVisuals[beatId] ?? []) + board.beats[index].allCandidates.filter {
            editorialDecision(beatId, $0.candidateId) == "rejected"
        }
        reserved += skipped.map { candidate in
            ["source_content_id": candidate.sourceContentId, "media_type": candidate.mediaType,
             "start_ms": candidate.startMs, "end_ms": candidate.endMs,
             "anchor_time_ms": candidate.anchorTimeMs as Any? ?? NSNull()] as [String: Any]
        }
        let script = FileManager.default.temporaryDirectory.appendingPathComponent("editorial-refresh-\(UUID().uuidString).txt")
        let selection = FileManager.default.temporaryDirectory.appendingPathComponent("editorial-reserved-\(UUID().uuidString).json")
        let savedBeats = FileManager.default.temporaryDirectory.appendingPathComponent("editorial-guidance-\(UUID().uuidString).json")
        do {
            try editorialGeneratedScript.write(to: script, atomically: true, encoding: .utf8)
            try JSONSerialization.data(withJSONObject: reserved).write(to: selection)
            let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
            try encoder.encode(board.beats.map(EditorialRefreshGuidance.init)).write(to: savedBeats)
        } catch {
            editorialRefreshInProgress = false
            editorialRefreshStatus = "全库复查失败：\(error.localizedDescription)"
            return
        }
        editorialRefreshStatus = "正在从当前素材库复查第 \(index + 1) 句，结合全文上下文并排除其他句已占用画面……"
        var arguments = ["editorial-board", "--script-file", script.path, "--track", editorialGeneratedTrack,
                         "--beat-id", beatId, "--reserved-file", selection.path, "--saved-beats-file", savedBeats.path]
        for guide in editorialGeneratedGuides where FileManager.default.fileExists(atPath: guide) { arguments += ["--guide-file", guide] }
        runHelper(arguments) { data, error in
            try? FileManager.default.removeItem(at: script)
            try? FileManager.default.removeItem(at: selection)
            try? FileManager.default.removeItem(at: savedBeats)
            self.editorialRefreshInProgress = false
            if generation == self.editorialGeneration {
                do {
                    if let error { throw NSError(domain: "Editorial", code: 3, userInfo: [NSLocalizedDescriptionKey: error]) }
                    guard let data, var current = self.editorialBoard else { return }
                    let update = try self.decoder().decode(EditorialBoardResponse.self, from: data)
                    if var replacement = update.beats.first, current.beats.indices.contains(index), current.beats[index].beatId == beatId {
                        let saved = current.beats[index].allCandidates.filter {
                            !self.editorialDecision(beatId, $0.candidateId).isEmpty && $0.isPlaceholder != true
                        }
                        let ids = Set(replacement.allCandidates.map { $0.candidateId })
                        replacement.candidates += saved.filter { !ids.contains($0.candidateId) }
                        let manual = saved.filter { $0.pool == "favorite_manual" }
                        let manualIds = Set(manual.map { $0.candidateId })
                        replacement.candidates.removeAll { manualIds.contains($0.candidateId) }
                        replacement.candidates.insert(contentsOf: manual, at: 0)
                        current.beats[index] = replacement
                        self.editorialBoard = current
                        self.autosaveEditorialSession()
                        let guideCount = self.editorialCandidates(for: index, channel: "guide").count
                        let count = self.editorialCandidates(for: index).count
                        self.editorialRefreshStatus = "第 \(index + 1) 句复查完成：初筛涉及 \(replacement.retrievalSourceCount) 个原文件（不代表全部适合）；排除 \(replacement.excludedVisualCount ?? 0) 个占用/跳过窗口。按表 \(guideCount) 个，系统补充 \(count) 个；可继续换批，不足时不强凑。"
                    }
                } catch { self.editorialRefreshStatus = "全库复查失败，保留已有候选：\(error.localizedDescription)" }
            }
            if self.editorialRefreshWanted { self.refreshEditorialBeat() }
        }
    }

    private func cleanEditorialPDF(_ document: PDFDocument, sourceURL: URL) -> (String, Int) {
        let pages: [[String]] = (0..<document.pageCount).map { pageIndex in
            (document.page(at: pageIndex)?.string ?? "")
                .replacingOccurrences(of: "\r\n", with: "\n")
                .replacingOccurrences(of: "\r", with: "\n")
                .components(separatedBy: "\n")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
        }
        let rawFileName = sourceURL.lastPathComponent
        let rawStem = sourceURL.deletingPathExtension().lastPathComponent
        func normalized(_ value: String) -> String {
            value.lowercased()
                .replacingOccurrences(
                    of: #"[^\p{L}\p{N}]"#, with: "", options: .regularExpression
                )
        }
        let fileName = normalized(rawFileName)
        let stem = normalized(rawStem)
        func resemblesFileTitle(_ value: String) -> Bool {
            let left = Array(value)
            let right = Array(stem)
            let shorter = min(left.count, right.count)
            guard shorter >= 10 else { return false }
            var common = 0
            while common < shorter && left[common] == right[common] { common += 1 }
            return common >= 10 && Double(common) / Double(shorter) >= 0.65
        }
        func mergeSoftWrappedLines(_ lines: [String]) -> [String] {
            var merged: [String] = []
            let terminal = #"[。！？!?；;】》」』”’）)]$"#
            let heading = #"^(第[一二三四五六七八九十0-9]+章|【|\[)"#
            for line in lines {
                let previous = merged.last ?? ""
                let previousFinished = previous.range(of: terminal, options: .regularExpression) != nil
                let previousIsHeading = previous.range(of: heading, options: .regularExpression) != nil
                let currentIsHeading = line.range(of: heading, options: .regularExpression) != nil
                if !merged.isEmpty && !previousFinished && !previousIsHeading && !currentIsHeading {
                    merged[merged.count - 1] += line
                } else {
                    merged.append(line)
                }
            }
            return merged
        }
        var edgeFrequency: [String: Int] = [:]
        for lines in pages {
            var seen = Set<String>()
            for (index, line) in lines.enumerated()
            where index < 3 || index >= max(0, lines.count - 3) {
                let key = normalized(line)
                if !key.isEmpty { seen.insert(key) }
            }
            for key in seen { edgeFrequency[key, default: 0] += 1 }
        }
        let repeatedThreshold = max(2, (document.pageCount + 1) / 2)
        var removed = 0
        var cleanPages: [String] = []
        for (pageIndex, lines) in pages.enumerated() {
            var kept: [String] = []
            for (index, line) in lines.enumerated() {
                let key = normalized(line)
                let atEdge = index < 3 || index >= max(0, lines.count - 3)
                let explicitPageNumber = line.range(
                    of: #"^(第\s*)?\d{1,4}\s*页$|^page\s*\d+(\s*(of|/)\s*\d+)?$"#,
                    options: [.regularExpression, .caseInsensitive]
                ) != nil
                let isolatedEdgeNumber = atEdge && line.range(
                    of: #"^[-—–_\s]*\d{1,4}(\s*/\s*\d{1,4})?[-—–_\s]*$"#,
                    options: .regularExpression
                ) != nil
                let fileNameLine = !stem.isEmpty && (
                    key == fileName || key == stem ||
                    (key.hasPrefix(stem) && key.count <= stem.count + 12) ||
                    (pageIndex == 0 && index < 3 && resemblesFileTitle(key))
                )
                let repeatedHeaderOrFooter = atEdge && key.count <= 100 &&
                    (edgeFrequency[key] ?? 0) >= repeatedThreshold
                if explicitPageNumber || isolatedEdgeNumber || fileNameLine || repeatedHeaderOrFooter {
                    removed += 1
                } else {
                    kept.append(line)
                }
            }
            let merged = mergeSoftWrappedLines(kept)
            if !merged.isEmpty { cleanPages.append(merged.joined(separator: "\n")) }
        }
        return (cleanPages.joined(separator: "\n"), removed)
    }

    private func stripEditorialChapterCards(_ text: String) -> (String, [String]) {
        let pattern = #"^第\s*[一二三四五六七八九十百千万零〇两0-9]+\s*章(?:\s*[:：·—-]\s*.*)?$"#
        let lines = text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
            .components(separatedBy: "\n")
        var body: [String] = []
        var chapterCards: [String] = []
        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.range(of: pattern, options: .regularExpression) != nil {
                chapterCards.append(trimmed)
            } else {
                body.append(line)
            }
        }
        return (body.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines), chapterCards)
    }

    func loadEditorialURL(_ url: URL) {
        let accessing = url.startAccessingSecurityScopedResource()
        defer { if accessing { url.stopAccessingSecurityScopedResource() } }
        do {
            let values = try url.resourceValues(forKeys: [.isDirectoryKey])
            if values.isDirectory == true {
                let entries = try FileManager.default.contentsOfDirectory(
                    at: url, includingPropertiesForKeys: [.isRegularFileKey],
                    options: [.skipsHiddenFiles]
                )
                editorialFolderFiles = entries.filter { ["txt", "pdf"].contains($0.pathExtension.lowercased()) }
                    .sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }
                let guideFiles = entries.filter { $0.pathExtension.lowercased() == "xlsx" }
                    .sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }
                editorialGuideFiles = guideFiles.map { $0.path }
                editorialGuideFile = guideFiles.first?.path ?? ""
                editorialGuideLabel = guideFiles.isEmpty ? "未载入逐句指导" : "已载入 \(guideFiles.count) 份指导：" + guideFiles.map { $0.lastPathComponent }.joined(separator: "、")
                editorialSourceLabel = "文件夹：\(url.lastPathComponent)（\(editorialFolderFiles.count) 个文稿）"
                editorialStatus = editorialFolderFiles.isEmpty ? "文件夹中没有 PDF 或 TXT 文稿。" : "请选择要载入的文稿；Excel 只提供当前项目的逐句剪辑指导。"
                return
            }
            let suffix = url.pathExtension.lowercased()
            if suffix == "xlsx" {
                editorialGuideFile = url.path
                if !editorialGuideFiles.contains(url.path) { editorialGuideFiles.append(url.path) }
                editorialGuideLabel = "已载入 \(editorialGuideFiles.count) 份指导：" + editorialGuideFiles.map { URL(fileURLWithPath: $0).lastPathComponent }.joined(separator: "、")
                editorialStatus = "Excel 已载入。它只辅助匹配当前项目，不会替换 PDF、TXT 或输入框中的真实口播文稿。"
                return
            }
            let text: String
            if suffix == "pdf" {
                guard let document = PDFDocument(url: url), document.pageCount > 0 else {
                    throw NSError(domain: "Editorial", code: 1, userInfo: [NSLocalizedDescriptionKey: "PDF 没有可提取的文字；扫描版 PDF 需要先做 OCR。"])
                }
                let cleaned = cleanEditorialPDF(document, sourceURL: url)
                text = cleaned.0
                editorialStatus = cleaned.1 > 0
                    ? "文稿已载入，并自动去掉 \(cleaned.1) 行文件名、页码或重复页眉页脚。"
                    : "文稿已载入，可以生成候选。"
            } else if suffix == "txt" {
                text = try String(contentsOf: url, encoding: .utf8)
                editorialStatus = "文稿已载入，可以生成候选。"
            } else {
                throw NSError(domain: "Editorial", code: 2, userInfo: [NSLocalizedDescriptionKey: "候选版当前只读取 PDF 和 UTF-8 TXT。"])
            }
            let stripped = stripEditorialChapterCards(text)
            editorialScript = stripped.0
            editorialChapterCards = stripped.1
            if !stripped.1.isEmpty {
                editorialStatus += " 已识别并排除 \(stripped.1.count) 个章节卡，它们只作为标题参考，不参与逐句选片。"
            }
            editorialSelectedFile = url.path
            editorialSourceLabel = "已载入：\(url.lastPathComponent)"
            if editorialScript.isEmpty { editorialStatus = "文稿没有可读取的文字。" }
        } catch {
            editorialStatus = "载入失败：\(error.localizedDescription)"
        }
    }

    func chooseEditorialFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true; panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.pdf, .plainText]
        if panel.runModal() == .OK, let url = panel.url { loadEditorialURL(url) }
    }

    func chooseEditorialGuide() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true; panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        if let xlsx = UTType(filenameExtension: "xlsx") {
            panel.allowedContentTypes = [xlsx]
        }
        if panel.runModal() == .OK { for url in panel.urls { loadEditorialURL(url) } }
    }

    func chooseEditorialFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { loadEditorialURL(url) }
    }

    func generateEditorialBoard() {
        do {
            if let current = captureEditorialSession() {
                try editorialWriter.flush()
                try EditorialSessionStore.save(current, to: editorialSessionDirectory.appendingPathComponent(current.sessionId + ".json"))
            }
        } catch { editorialSessionStatus = "原工程尚未保存，已暂停重新生成：\(error.localizedDescription)"; return }
        let stripped = stripEditorialChapterCards(editorialScript)
        let text = stripped.0
        if !stripped.1.isEmpty {
            editorialChapterCards = stripped.1
            editorialScript = text
        }
        guard !text.isEmpty else { editorialStatus = "请先输入或载入文稿。"; return }
        editorialLoading = true
        editorialGeneration = UUID()
        let generatedGuides = editorialGuideFiles.isEmpty ? (editorialGuideFile.isEmpty ? [] : [editorialGuideFile]) : editorialGuideFiles
        let generatedTrack = editorialTrack
        editorialRefreshStatus = ""
        editorialStatus = "正在只读检索当前素材库……"
        let temp = FileManager.default.temporaryDirectory
            .appendingPathComponent("editorial-script-\(UUID().uuidString).txt")
        do { try text.write(to: temp, atomically: true, encoding: .utf8) }
        catch { editorialLoading = false; editorialStatus = "无法准备文稿：\(error.localizedDescription)"; return }
        var arguments = ["editorial-board", "--script-file", temp.path, "--track", editorialTrack]
        for guide in generatedGuides { arguments += ["--guide-file", guide] }
        runHelper(arguments) { data, error in
            try? FileManager.default.removeItem(at: temp)
            self.editorialLoading = false
            if let error { self.editorialStatus = "生成失败：\(error)"; return }
            guard let data else { self.editorialStatus = "生成失败：没有返回数据。"; return }
            do {
                let board = try self.decoder().decode(EditorialBoardResponse.self, from: data)
                self.editorialSessionRestoring = true
                self.editorialSessionId = UUID().uuidString
                self.editorialGeneratedScript = text
                self.editorialGeneratedGuides = generatedGuides
                self.editorialGeneratedTrack = generatedTrack
                self.editorialBoard = board
                self.editorialReviewReturnBeatId = nil
                self.editorialActiveBeat = 0
                self.editorialDecisions = [:]
                self.editorialCutOverrides = [:]
                self.editorialLockedCuts = []
                self.editorialSkippedVisuals = [:]
                self.editorialSessionRestoring = false
                self.autosaveEditorialSession()
                self.editorialDecisionStatus = "生成已完整结束。入选与备选都会显示在候选箱，点击同一按钮可取消。"
                let ignored = max(self.editorialChapterCards.count, board.ignoredChapterCards?.count ?? 0)
                let chapterNote = ignored > 0 ? "；已排除 \(ignored) 个章节卡" : ""
                let guideNote: String
                if let guide = board.editorialGuideSummary {
                    guideNote = "；项目指导匹配 \(guide.matchedBeatCount ?? 0)/\(board.beats.count) 句"
                } else {
                    guideNote = ""
                }
                self.editorialStatus = "已从当前素材库生成 \(board.beats.count) 句候选\(chapterNote)\(guideNote)；数据库只读，未调用模型。"
            } catch { self.editorialStatus = "无法读取候选结果：\(error.localizedDescription)" }
        }
    }

    private func editorialExportPayload() -> [String: Any]? {
        guard let board = editorialBoard else { return nil }
        for beat in board.beats {
            for candidate in editorialSavedCandidates(for: beat) {
                let range = editorialCut(beat, candidate)
                guard range.allSatisfy({ $0.isFinite && $0 >= 0 && $0 < 86_400_000 }), range[1] > range[0] else { return nil }
            }
        }
        var decisions: [[String: Any]] = []
        for beat in board.beats {
            for candidate in beat.allCandidates + [beat.aRollOption].compactMap({ $0 }) {
                let value = editorialDecision(beat.beatId, candidate.candidateId)
                if !value.isEmpty {
                    decisions.append([
                        "beat_id": beat.beatId, "candidate_id": candidate.candidateId,
                        "role": candidate.role, "decision": value,
                    ])
                }
            }
        }
        let beats: [[String: Any]] = board.beats.map { beat in
            let exportChoices = beat.allCandidates + [beat.aRollOption].compactMap({ $0 })
            let candidates: [[String: Any]] = exportChoices.map { candidate in
                let range = editorialCut(beat, candidate)
                return [
                    "candidate_id": candidate.candidateId,
                    "source_content_id": candidate.sourceContentId,
                    "source_file": candidate.sourceFile,
                    "media_type": candidate.mediaType,
                    "role": candidate.role,
                    "editorial_function": candidate.editorialFunction ?? candidate.role,
                    "narrative_intent": candidate.narrativeIntent ?? beat.narrativeIntent ?? "",
                    "recommendation_reason": candidate.recommendationReason ?? candidate.fitReason,
                    "manual_origin": ["favorite_manual", "search_manual"].contains(candidate.pool) ? candidate.pool : "",
                    "fit_reason": candidate.fitReason,
                    "start_ms": candidate.startMs,
                    "end_ms": candidate.endMs,
                    "provisional_in_ms": Int((range[0] * 1000).rounded()),
                    "provisional_out_ms": Int((range[1] * 1000).rounded()),
                    "cut_locked": editorialLockedCuts.contains(editorialKey(beat.beatId, candidate.candidateId)),
                    "cut_origin": editorialLockedCuts.contains(editorialKey(beat.beatId, candidate.candidateId)) ? "human_preview_confirmed" : (editorialCutOverrides[editorialKey(beat.beatId, candidate.candidateId)] != nil ? "manual" : "suggested"),
                    "cinematic_scores": candidate.cinematicScores ?? [:],
                    "cinematic_penalties": candidate.cinematicPenalties ?? [:],
                    "cinematic_final_score": candidate.cinematicFinalScore ?? 0,
                    "actual_primary_subject": candidate.actualPrimarySubject ?? "UNKNOWN",
                    "candidate_shot_role": candidate.candidateShotRole ?? "UNKNOWN",
                    "gate_status": candidate.gateStatus ?? "PASS",
                    "guide_source_label": candidate.guideSourceLabel ?? "",
                    "guide_source_tier": candidate.guideSourceTier ?? 4,
                    "gate_penalty": candidate.gatePenalty ?? 0,
                    "gate_reason_codes": candidate.gateReasonCodes ?? [],
                    "subject_match_score": candidate.subjectMatchScore ?? 0,
                    "shot_role_match_score": candidate.shotRoleMatchScore ?? 0,
                    "evidence_score": candidate.evidenceScore ?? 0,
                    "truthfulness_score": candidate.truthfulnessScore ?? 0,
                    "requires_source_review": candidate.requiresSourceReview ?? false,
                    "shot_scale": candidate.shotScale,
                    "composition": candidate.composition,
                    "camera_angle": candidate.cameraAngle,
                    "visual_strategy": [
                        "narrative_intent": candidate.narrativeIntent ?? beat.narrativeIntent ?? "",
                        "shot_brief": beat.shotBrief ?? "",
                    ],
                ]
            }
            let projectGuidance: [String: Any] = [
                "match_confidence": beat.projectEditorialGuidance?.matchConfidence ?? 0,
                "match_type": beat.projectEditorialGuidance?.matchType ?? "",
                "excel_rows": beat.projectEditorialGuidance?.excelRows ?? [],
                "section": beat.projectEditorialGuidance?.section ?? "",
                "guide_narration": beat.projectEditorialGuidance?.guideNarration ?? "",
                "primary_shot": beat.projectEditorialGuidance?.primaryShot ?? "",
                "visual_direction": beat.projectEditorialGuidance?.visualDirection ?? "",
                "alternative_shot": beat.projectEditorialGuidance?.alternativeShot ?? "",
                "editing_method": beat.projectEditorialGuidance?.editingMethod ?? "",
                "notes": beat.projectEditorialGuidance?.notes ?? "",
                "guidance_status": beat.projectEditorialGuidance?.guidanceStatus ?? "",
            ]
            let fallbackPlan: [String: Any] = [
                "content_requirement": beat.fallbackPlan?.contentRequirement ?? "",
                "aesthetic_requirement": beat.fallbackPlan?.aestheticRequirement ?? "",
                "editing_responsibility": beat.fallbackPlan?.editingResponsibility ?? "",
                "capture_suggestion": beat.fallbackPlan?.captureSuggestion ?? "",
            ]
            return [
                "beat_id": beat.beatId, "order": beat.order, "text": beat.text,
                "narrative_intent": beat.narrativeIntent ?? "",
                "visual_strategy": [
                    "narrative_intent": beat.narrativeIntent ?? "",
                    "shot_brief": beat.shotBrief ?? "",
                ],
                "selection_requirement": [
                    "visual_task": beat.visualTask ?? "UNKNOWN",
                    "expected_primary_subject": beat.expectedPrimarySubject ?? "UNKNOWN",
                    "preferred_shot_roles": beat.preferredShotRoles ?? [],
                    "visualizability": beat.visualizability ?? "NEEDS_HUMAN_REVIEW",
                    "a_roll_preference": beat.aRollPreference ?? "LOW",
                    "sound_instruction": beat.soundInstruction ?? false,
                ],
                "project_editorial_guidance": projectGuidance,
                "guide_match_status": beat.guideMatchStatus ?? "UNKNOWN",
                "guide_search_message": beat.guideSearchMessage ?? "",
                "fallback_plan": fallbackPlan,
                "gap_status": [
                    "available": beat.gapStatus?.available ?? true,
                    "recommended": beat.gapStatus?.recommended ?? false,
                    "candidate_slots_consumed": 0,
                    "reason": beat.gapStatus?.reason ?? "缺口不占真实素材候选名额",
                ],
                "candidates": candidates,
            ]
        }
        let guideSummary: [String: Any] = [
            "source_file": board.editorialGuideSummary?.sourceFile ?? "",
            "sheet_name": board.editorialGuideSummary?.sheetName ?? "",
            "guide_row_count": board.editorialGuideSummary?.guideRowCount ?? 0,
            "matched_beat_count": board.editorialGuideSummary?.matchedBeatCount ?? 0,
            "unmatched_beat_count": board.editorialGuideSummary?.unmatchedBeatCount ?? 0,
        ]
        return [
            "track": editorialGeneratedTrack, "timeline_name": editorialTimelineName,
            "guide_files": editorialGeneratedGuides,
            "frame_rate": editorialFrameRate,
            "include_backups": editorialIncludeBackups,
            "editorial_guide_summary": guideSummary,
            "beats": beats,
            "decisions": decisions,
            "browsing_skips": editorialSkippedVisuals.mapValues { $0.map { $0.candidateId } },
        ]
    }

    func exportEditorialJSON() {
        guard let payload = editorialExportPayload() else { editorialExportStatus = "请先生成候选，并检查候选箱出点晚于入点、数值有效。"; return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.json]
        panel.nameFieldStringValue = "\(editorialTimelineName).json"
        guard panel.runModal() == .OK, let output = panel.url else { return }
        let request = FileManager.default.temporaryDirectory
            .appendingPathComponent("editorial-manifest-\(UUID().uuidString).json")
        do {
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: request, options: .atomic)
        } catch { editorialExportStatus = "无法准备剪辑清单：\(error.localizedDescription)"; return }
        editorialExportStatus = "正在生成带选片理由和分项评分的剪辑清单……"
        runHelper(["editorial-manifest", "--request-file", request.path, "--output", output.path]) { data, error in
            try? FileManager.default.removeItem(at: request)
            if let error { self.editorialExportStatus = "导出失败：\(error)"; return }
            self.editorialExportStatus = data == nil ? "导出失败：没有返回结果。" : "剪辑清单已导出：\(output.lastPathComponent)"
        }
    }

    func exportEditorialTimeline() {
        guard let payload = editorialExportPayload() else { editorialExportStatus = "请先生成候选，并检查候选箱出点晚于入点、数值有效。"; return }
        let selectedCount = editorialDecisions.values.filter { $0 == "selected" }.count
        let backupCount = editorialIncludeBackups ? editorialDecisions.values.filter { $0 == "review" }.count : 0
        guard selectedCount + backupCount > 0 else { editorialExportStatus = "至少加入一个主选或备选后才能导出时间线。"; return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [UTType(filenameExtension: "fcpxml") ?? .xml]
        panel.nameFieldStringValue = "\(editorialTimelineName).fcpxml"
        guard panel.runModal() == .OK, let output = panel.url else { return }
        let request = FileManager.default.temporaryDirectory
            .appendingPathComponent("editorial-timeline-\(UUID().uuidString).json")
        do {
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted])
            try data.write(to: request, options: .atomic)
        } catch { editorialExportStatus = "无法准备导出清单：\(error.localizedDescription)"; return }
        editorialExportStatus = "正在核对原片并生成时间线……"
        runHelper(["editorial-timeline", "--request-file", request.path, "--output", output.path]) { data, error in
            try? FileManager.default.removeItem(at: request)
            if let error { self.editorialExportStatus = "导出失败：\(error)"; return }
            guard let data, let report = try? JSONSerialization.jsonObject(with: data) as? [String: Any], report["status"] as? String == "PASS" else {
                self.editorialExportStatus = "导出失败：没有返回成功凭据。"; return
            }
            let sources = report["verified_source_count"] as? Int ?? 0
            let placeholderOnly = report["placeholder_only"] as? Bool ?? false
            self.editorialExportStatus = placeholderOnly
                ? "已导出纯占位 XML：只有黑屏/口播占位和文稿标记，没有视频。无需素材盘，但这不是含原片的粗剪。文件：\(output.path)"
                : "FCPXML 已导出：已核对 \(sources) 个原片；V1 主选 \(report["selected_clip_count"] as? Int ?? 0)及黑屏占位，V2 禁用备选 \(backupCount)，V3 文稿参考 \(report["script_reference_count"] as? Int ?? 0)句（默认禁用，可在检查器阅读或启用）。XML 只记录路径和剪点，不包含视频。文件：\(output.path)"
        }
    }

    func reveal(_ result: SearchResult) {
        guard let path = result.sourcePath, !path.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func pausePreviewWindows() {
        for controller in videoPreviewControllers {
            if let preview = controller as? EditorialPlaybackWindowController { preview.playback.player.pause() }
            if let preview = controller as? VideoPreviewWindowController { preview.player?.pause() }
        }
    }

    func closePreviewWindows() {
        // Detach first: close callbacks may otherwise mutate the collection being iterated.
        let controllers = videoPreviewControllers
        videoPreviewControllers.removeAll()
        for controller in controllers { controller.close() }
    }

    func previewEditorial(_ beat: EditorialBeat, _ candidate: EditorialCandidate, context: Bool = false) {
        guard let board = editorialBoard, candidate.isPlaceholder != true else { return }
        let key = editorialKey(beat.beatId, candidate.candidateId)
        guard !editorialPreviewPending.contains(key) else { return }
        let generation = editorialGeneration
        let range = editorialCut(beat, candidate)
        editorialPreviewPending.insert(key)
        editorialPreviewStatus[key] = "正在检查原片路径……"
        runHelper(["editorial-preview", "--candidate-id", candidate.candidateId,
                   "--source-content-id", candidate.sourceContentId, "--board-database", board.database]) { data, error in
            self.editorialPreviewPending.remove(key)
            guard self.editorialGeneration == generation else { return }
            if let error {
                self.editorialPreviewStatus[key] = "无法播放：\(error.isEmpty ? "原片检查未成功，请检查素材库和素材盘后重试。" : error)"; return
            }
            guard let data, let source = try? self.decoder().decode(EditorialPreviewResponse.self, from: data) else {
                self.editorialPreviewStatus[key] = "无法播放：原片路径返回无效。"; return
            }
            let url = URL(fileURLWithPath: source.sourcePath)
            if source.mediaType == "image" {
                self.editorialPreviewStatus[key] = NSWorkspace.shared.open(url) ? "已打开原始图片。" : "无法打开原始图片，请检查默认图片查看器。"
            } else {
                self.videoPreviewControllers.removeAll { !($0.window?.isVisible ?? false) }
                // Avoid simultaneous audio when opening another candidate.
                for controller in self.videoPreviewControllers {
                    if let preview = controller as? EditorialPlaybackWindowController { preview.playback.player.pause() }
                }
                let controller = EditorialPlaybackWindowController(url: url, range: range, context: context,
                    locked: self.editorialLockedCuts.contains(key)) { [weak self] confirmed, decision in
                    guard let self, self.editorialGeneration == generation,
                          let current = self.editorialBoard,
                          let liveBeat = current.beats.first(where: { $0.beatId == beat.beatId }),
                          let liveCandidate = liveBeat.allCandidates.first(where: { $0.candidateId == candidate.candidateId }) else {
                        return "文稿或候选已变化，请关闭窗口，从当前候选重新打开再确认。"
                    }
                    if current.beats.filter({ $0.beatId != beat.beatId }).contains(where: { other in
                        self.editorialSavedCandidates(for: other).contains(where: { self.sameEditorialVisual($0, liveCandidate) })
                    }) { return "这个画面已被其他句占用；请先处理原来的选择，再确认这条。" }
                    self.editorialCutOverrides[key] = confirmed
                    self.editorialLockedCuts.insert(key)
                    if self.editorialDecision(beat.beatId, candidate.candidateId) != decision {
                        self.setEditorialDecision(beat: liveBeat, candidate: liveCandidate, decision: decision)
                    }
                    self.editorialDecisionStatus = "第 \(beat.order) 句剪点已确认并锁定；重新推荐不会改动它。导出 JSON 可留档。"
                    return nil
                }
                self.videoPreviewControllers.append(controller)
                controller.onClosed = { [weak self, weak controller] in
                    guard let self, let controller else { return }
                    self.videoPreviewControllers.removeAll { $0 === controller }
                    if self.editorialGeneration == generation {
                        self.editorialPreviewStatus[key] = "预览已关闭，内置播放器与原片已释放。"
                    }
                }
                controller.showWindow(nil)
                NSApp.activate(ignoringOtherApps: true)
                self.editorialPreviewStatus[key] = "原片预览窗口已打开；播放或解码结果请看窗口提示。"
            }
        }
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
            controller.onClosed = { [weak self, weak controller] in
                guard let self, let controller else { return }
                self.videoPreviewControllers.removeAll { $0 === controller }
            }
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
      ScrollView {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                BrandIcon(size: 48)
                VStack(alignment: .leading) { Text(bundledAppName).font(.title3.bold()); Text("本地素材整理与搜索").font(.caption).foregroundStyle(archiveMuted) }
            }.padding(.bottom, 22)
            ForEach(ArchivePage.allCases) { page in
                Button { model.openMainPage(page) } label: {
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
        }.padding(24)
      }.frame(width: 250).background(Color.white.opacity(0.78))
    }
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

struct EditorialPreviewButtons: View {
    @EnvironmentObject var model: ArchiveModel
    let beat: EditorialBeat
    let candidate: EditorialCandidate
    var body: some View {
        if candidate.isPlaceholder != true {
            let key = model.editorialKey(beat.beatId, candidate.candidateId)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Button { model.previewEditorial(beat, candidate) } label: {
                        Label(candidate.mediaType == "image" ? "查看原图" : "播放建议片段", systemImage: "play.circle.fill")
                    }
                    if candidate.mediaType != "image" {
                        Button("查看原片前后文") { model.previewEditorial(beat, candidate, context: true) }
                    }
                }.disabled(model.editorialPreviewPending.contains(key))
                if let status = model.editorialPreviewStatus[key] {
                    Text(status).font(.caption2).foregroundStyle(status.contains("无法") ? .red : archiveBlue).textSelection(.enabled)
                }
            }.font(.caption)
        }
    }
}

struct EditorialChannelSection: View {
    @EnvironmentObject var model: ArchiveModel
    let beat: EditorialBeat
    let index: Int
    let channel: String
    var body: some View {
        let rows = model.editorialCandidates(for: index, channel: channel)
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Text(channel == "guide" ? "A · 按逐句表定位" : "B · 系统补充建议").font(.headline)
                Spacer()
                Button("这组不合适，换一批") { model.nextEditorialBatch(channel) }.font(.caption)
            }
            Text(channel == "guide"
                 ? "只放指导表指定编号/日期范围内且有内容依据的画面；没有则留空，不拿别日素材充数。"
                 : "结合本句、前后文和剪辑职责寻找补充画面；不与上方当前画面重复。")
                .font(.caption2).foregroundStyle(archiveMuted)
            if rows.isEmpty {
                Text(channel == "guide" ? "本批未找到符合表内定位条件的可用画面。可以换批继续查，或核对编号与原文件的对应关系。" : "本批没有新的系统补充候选。可以继续换批；仍无结果时保留缺口，由你判断。")
                    .font(.caption).foregroundStyle(archiveOrange)
            }
            ForEach(rows) { EditorialCandidateCard(beat: beat, candidate: $0) }
        }.padding(.vertical, 6)
    }
}

// Decode only existing preview images, off the UI thread. Scrolling/resizing
// must never reopen and decode the same image in a SwiftUI body evaluation.
private enum EditorialThumbnailCache {
    static let images: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>()
        cache.countLimit = 96; cache.totalCostLimit = 32 * 1024 * 1024
        return cache
    }()
    static let queue = DispatchQueue(label: "editorial.thumbnail.decode", qos: .utility)
    static func load(_ path: String) async -> NSImage? {
        guard !path.isEmpty else { return nil }
        if let cached = images.object(forKey: path as NSString) { return cached }
        return await withCheckedContinuation { continuation in
            queue.async {
                if let cached = images.object(forKey: path as NSString) { continuation.resume(returning: cached); return }
                guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary),
                      let bitmap = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                        kCGImageSourceShouldCacheImmediately: true,
                        kCGImageSourceThumbnailMaxPixelSize: 360
                      ] as CFDictionary) else { continuation.resume(returning: nil); return }
                let image = NSImage(cgImage: bitmap, size: NSSize(width: bitmap.width, height: bitmap.height))
                images.setObject(image, forKey: path as NSString, cost: bitmap.bytesPerRow * bitmap.height)
                continuation.resume(returning: image)
            }
        }
    }
}

private struct EditorialThumbnail: View {
    let path: String
    var contentMode: ContentMode = .fit
    @State private var image: NSImage?
    var body: some View {
        Group {
            if let image { Image(nsImage: image).resizable().aspectRatio(contentMode: contentMode) }
            else { ZStack { Color.gray.opacity(0.12); Image(systemName: "photo").foregroundStyle(.secondary) } }
        }.task(id: path) {
            image = nil
            let loaded = await EditorialThumbnailCache.load(path)
            if !Task.isCancelled { image = loaded }
        }
    }
}

// A full-width, immediately labelled button instead of a tiny disclosure arrow.
// The content closure is evaluated only when open; large guidance/candidate
// explanations do not build hidden view trees on every selection update.
struct EditorialDisclosure<Content: View>: View {
    let title: String
    let content: () -> Content
    @State private var expanded = false
    @State private var lastToggle = Date.distantPast
    init(_ title: String, initiallyExpanded: Bool = false, @ViewBuilder content: @escaping () -> Content) {
        self.title = title; self.content = content
        _expanded = State(initialValue: initiallyExpanded)
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                let now = Date()
                guard now.timeIntervalSince(lastToggle) >= 0.35 else { return }
                lastToggle = now; expanded.toggle()
            } label: {
                HStack(spacing: 9) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right").frame(width: 18)
                    Text(title).multilineTextAlignment(.leading)
                    Spacer(minLength: 8)
                    Text(expanded ? "收起" : "展开").foregroundStyle(archiveBlue)
                }.padding(.horizontal, 10).frame(minHeight: 40).frame(maxWidth: .infinity)
                 .contentShape(Rectangle()).background(Color.blue.opacity(0.06)).cornerRadius(6)
            }.buttonStyle(.plain).accessibilityLabel(title + (expanded ? "，收起" : "，展开"))
            if expanded { content() }
        }.transaction { $0.animation = nil }
    }
}

struct EditorialCandidateCard: View {
    @EnvironmentObject var model: ArchiveModel
    let beat: EditorialBeat
    let candidate: EditorialCandidate
    var body: some View {
        Panel {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top, spacing: 12) {
                    EditorialThumbnail(path: candidate.previewPath)
                        .frame(width: 180, height: 105).clipped().clipShape(RoundedRectangle(cornerRadius: 7))
                    VStack(alignment: .leading, spacing: 5) {
                        Text(candidate.description.isEmpty ? candidate.displayTitle : candidate.description)
                            .font(.headline).lineLimit(4)
                        Text(candidate.sourceFile).font(.caption2).foregroundStyle(archiveMuted).lineLimit(2)
                        let range = model.editorialCut(beat, candidate)
                        Text(String(format: "试用 %.2f 秒｜原片 %.2f → %.2f 秒 · %@", range[1] - range[0], range[0], range[1], candidate.role))
                            .font(.caption).foregroundStyle(archiveBlue)
                    }
                }
                EditorialPreviewButtons(beat: beat, candidate: candidate)
                HStack(spacing: 6) {
                    ForEach(candidate.shotScale + candidate.composition + candidate.cameraAngle, id: \.self) { tag in
                        Text(tag).font(.caption2).padding(.horizontal, 7).padding(.vertical, 3)
                            .background(Color.blue.opacity(0.08)).clipShape(Capsule())
                    }
                }
                EditorialDisclosure("为什么推荐 · 视听语言与使用边界") {
                  VStack(alignment: .leading, spacing: 6) {
                    if let intent = candidate.narrativeIntent, !intent.isEmpty {
                        Text(model.editorialText("本句意图：\(intent) · 镜头职责：\(candidate.editorialFunction ?? candidate.role) · 综合选片分：\(String(format: "%.1f", candidate.cinematicFinalScore ?? 0))"))
                            .font(.subheadline.bold()).foregroundStyle(archiveBlue)
                    }
                    Text(model.editorialText("适配检查：\(candidate.gateStatus ?? "PASS") · 需要主体：\(beat.expectedPrimarySubject ?? "UNKNOWN") · 实际主体：\(candidate.actualPrimarySubject ?? "UNKNOWN")"))
                        .font(.subheadline.bold())
                        .foregroundStyle(candidate.gateStatus == "SOFT_GATE" ? archiveOrange : archiveGreen)
                    Text("为什么入围").font(.subheadline.bold())
                    if let sourceLabel = candidate.guideSourceLabel, !sourceLabel.isEmpty {
                        Text("素材定位：\(sourceLabel)").foregroundStyle(archiveBlue)
                    }
                    Text(model.editorialText(candidate.recommendationReason?.isEmpty == false ? candidate.recommendationReason! : (candidate.fitReason.isEmpty ? candidate.matchReasons.joined(separator: "；") : candidate.fitReason)))
                    if let gateReasons = candidate.gateReasons, !gateReasons.isEmpty {
                        Text(model.editorialText("限制：\(gateReasons.joined(separator: "；"))"))
                    }
                    if let rankReason = candidate.rankReason, !rankReason.isEmpty {
                        Text(model.editorialText("当前名次：\(rankReason)"))
                    }
                    Text("视听语言与剪辑作用").font(.subheadline.bold())
                    Text(model.editorialText(candidate.visualLanguage.isEmpty ? "需要结合原片前后帧复核景别、构图和动作连续性。" : candidate.visualLanguage))
                    Text("使用边界").font(.subheadline.bold())
                    Text(model.editorialText(candidate.fitBoundary.isEmpty ? candidate.risks.joined(separator: "；") : candidate.fitBoundary))
                    Text(model.editorialText("剪辑建议：\(candidate.editingMethod.isEmpty ? "按句意重音设置出入点，并试听同期声" : candidate.editingMethod)"))
                        .foregroundStyle(archiveMuted)
                  }
                }.font(.caption)
                HStack {
                    decisionButton("入选", value: "selected", color: archiveGreen)
                    decisionButton("备选", value: "review", color: archiveOrange)
                    decisionButton("排除", value: "rejected", color: .red)
                    Spacer()
                    Text(candidate.evidenceMode == "verified_video" ? "已验证原片" : "基于数据库抽样帧，需回看原片")
                        .font(.caption2).foregroundStyle(archiveMuted)
                }
            }
        }
    }

    @ViewBuilder private func decisionButton(_ title: String, value: String, color: Color) -> some View {
        let active = model.editorialDecision(beat.beatId, candidate.candidateId) == value
        Button(title) { model.setEditorialDecision(beat: beat, candidate: candidate, decision: value) }
            .buttonStyle(.borderedProminent).tint(active ? color : Color.gray.opacity(0.45))
    }
}

struct EditorialSelectionRow: View {
    @EnvironmentObject var model: ArchiveModel
    let beat: EditorialBeat
    let candidate: EditorialCandidate
    var body: some View {
        let state = model.editorialDecision(beat.beatId, candidate.candidateId)
        let selected = state == "selected"
        let title = selected ? "主选" : "备选"
        VStack(alignment: .leading, spacing: 3) {
            Button("\(title) · 第 \(beat.order) 句 · 回看修改") { model.reviewEditorialBeat(beat.beatId) }
                .buttonStyle(.plain).font(.caption.bold())
                .foregroundStyle(selected ? archiveGreen : archiveOrange)
                .help("只跳转，不改变入选或备选；修改后可返回原来的选片进度。")
            Text(beat.text).font(.caption).lineLimit(2)
            Text(candidate.sourceFile.isEmpty ? candidate.displayTitle : candidate.sourceFile)
                .font(.caption2).foregroundStyle(archiveMuted).lineLimit(2)
            let range = model.editorialCut(beat, candidate)
            Text(String(format: "%@｜使用 %.2f 秒", selected ? "V1 主选" : "V2 备选（禁用）", range[1] - range[0])).font(.caption.bold())
            HStack {
                Text("入点秒").font(.caption2)
                TextField("入点", value: model.editorialCutBinding(beat, candidate, 0), format: .number.precision(.fractionLength(2)))
                Text("出点秒").font(.caption2)
                TextField("出点", value: model.editorialCutBinding(beat, candidate, 1), format: .number.precision(.fractionLength(2)))
            }.textFieldStyle(.roundedBorder).font(.caption)
                .disabled(model.editorialLockedCuts.contains(model.editorialKey(beat.beatId, candidate.candidateId)))
            if model.editorialLockedCuts.contains(model.editorialKey(beat.beatId, candidate.candidateId)) {
                Label("剪点已锁定 · 打开播放器可解锁调整", systemImage: "lock.fill").font(.caption2)
            }
            Text("可手调；原片未核对。导出按目标帧率对齐，不能超过原片长度。").font(.caption2).foregroundStyle(archiveMuted)
            EditorialPreviewButtons(beat: beat, candidate: candidate)
            Button("取消这条\(title)") {
                model.setEditorialDecision(beat: beat, candidate: candidate, decision: state)
            }.font(.caption2)
        }.padding(7)
            .background(selected ? Color.green.opacity(0.08) : Color.orange.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

// The scrolling list is an index, not 100 simultaneously live editing forms.
// Fixed-height summaries avoid repeatedly measuring multiline controls during
// scroll/resize. The full, existing editor is constructed only in one popover.
struct EditorialSelectionSummaryRow: View {
    @EnvironmentObject var model: ArchiveModel
    let selection: EditorialSavedSelection
    let edit: () -> Void
    var body: some View {
        let beat = selection.beat, candidate = selection.candidate
        let selected = model.editorialDecision(beat.beatId, candidate.candidateId) == "selected"
        let range = model.editorialCut(beat,candidate)
        VStack(alignment: .leading, spacing: 4) {
            Button { model.reviewEditorialBeat(beat.beatId) } label: {
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(selected ? "主选" : "备选") · 第 \(beat.order) 句 · 回看修改").font(.caption.bold())
                    Text(beat.text).font(.caption).foregroundStyle(.primary).lineLimit(1)
                    Text(candidate.sourceFile.isEmpty ? candidate.displayTitle : candidate.sourceFile)
                        .font(.caption2).foregroundStyle(archiveMuted).lineLimit(1).truncationMode(.middle)
                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain).foregroundStyle(selected ? archiveGreen : archiveOrange)
            HStack(spacing: 4) {
                Text(String(format:"%.2f → %.2f 秒",range[0],range[1])).font(.caption2.monospacedDigit())
                if model.editorialLockedCuts.contains(selection.id) { Image(systemName:"lock.fill").font(.caption2) }
                Spacer(minLength: 0)
            }
            HStack {
                if candidate.isPlaceholder != true {
                    Button("播放") { model.previewEditorial(beat,candidate) }
                        .disabled(model.editorialPreviewPending.contains(selection.id))
                }
                Button("剪点 / 操作…", action: edit)
            }.font(.caption2)
        }.padding(7).frame(maxWidth: .infinity, alignment: .leading).frame(height: 112)
         .background(selected ? Color.green.opacity(0.08) : Color.orange.opacity(0.08))
         .clipShape(RoundedRectangle(cornerRadius:6))
    }
}

// Called on navigation/choice changes, never on each scroll or save-status tick.
enum EditorialSelectionFocus {
    static func targetID(_ rows: [EditorialSavedSelection], activeOrder: Int) -> String? {
        rows.last(where: { $0.beat.order <= activeOrder })?.id ?? rows.first?.id
    }
}

struct EditorialPage: View {
    @EnvironmentObject var model: ArchiveModel
    @State private var editingSelection: EditorialSavedSelection?
    @State private var inputPresented = false
    @State private var exportPresented = false
    var body: some View {
      GeometryReader { geometry in
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("文稿选片（实验）").font(.title2.bold())
                    Text("逐句找画面 · 人工选主选与备选 · 导出粗剪")
                        .font(.caption).foregroundStyle(archiveMuted)
                }
                Spacer(minLength: 8)
                if model.editorialBoard != nil {
                    Button("文稿与指导 · 步骤 1") { inputPresented = true }
                        .help("步骤一已收起；点击查看文稿、导入文件或修改指导。")
                }
            }.fixedSize(horizontal: false, vertical: true)
            if model.editorialBoard == nil {
                inputPanel.frame(maxWidth: 680).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                HStack(alignment: .top, spacing: 12) {
                    candidatePanel.frame(maxWidth: .infinity, maxHeight: .infinity)
                    decisionPanel.frame(width: min(340, max(280, geometry.size.width * 0.28)))
                        .frame(maxHeight: .infinity)
                }.frame(maxHeight: .infinity).layoutPriority(1)
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("步骤 4 · 剪辑清单与时间线").font(.headline)
                        Text("完成这一轮选择后，再设置帧率并导出。未选完也可保存工程。")
                            .font(.caption).foregroundStyle(archiveMuted)
                    }
                    Spacer(minLength: 8)
                    Button("打开导出设置…") { exportPresented = true }
                        .buttonStyle(PrimaryButtonStyle())
                }.padding(14).background(Color.white).cornerRadius(10)
                 .fixedSize(horizontal: false, vertical: true)
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
      }.padding(18).disabled(model.editorialLoading)
        .sheet(isPresented: $inputPresented) {
            VStack(spacing: 12) {
                HStack {
                    Text("步骤 1 · 文稿与项目指导").font(.headline)
                    Spacer(); Button("收起步骤 1，继续选片") { inputPresented = false }
                        .keyboardShortcut(.cancelAction)
                }
                inputPanel
            }.padding(18).frame(width: 680, height: 540).environmentObject(model)
        }
        .sheet(isPresented: $exportPresented) {
            VStack(spacing: 12) {
                HStack {
                    Text("步骤 4 · 剪辑清单与时间线").font(.headline)
                    Spacer(); Button("返回选片") { exportPresented = false }.keyboardShortcut(.cancelAction)
                }
                exportPanel
            }.padding(18).frame(width: 720, height: 520).environmentObject(model)
        }
        .onChange(of: model.editorialBoard != nil) { loaded in
            if loaded { inputPresented = false }
        }
        // Selection filters the cached queues immediately. Database work only
        // happens on explicit generate/recheck or when a requested batch runs out.
        .onDrop(of: [UTType.fileURL], isTargeted: nil) { providers in
            guard let provider = providers.first else { return false }
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                let url: URL?
                if let data = item as? Data { url = URL(dataRepresentation: data, relativeTo: nil) }
                else { url = item as? URL }
                if let url { DispatchQueue.main.async { model.loadEditorialURL(url) } }
            }
            return true
        }
    }

    private var inputPanel: some View {
        Panel {
          ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text("步骤 1 · 文稿与项目指导").font(.headline)
                HStack {
                    Button("打开工程 / 旧版清单") { model.openEditorialProject() }
                    Button("继续最近工程") { model.continueRecentEditorialProject() }
                }.font(.caption).disabled(model.editorialLoading)
                Button("保存未完成工程副本") { model.saveEditorialProjectCopy() }
                    .font(.caption).disabled(model.editorialBoard == nil || model.editorialLoading)
                Button("从我的收藏补选当前句…") { model.openEditorialFavorites() }
                    .disabled(model.editorialBoard == nil || model.editorialLoading)
                Text(model.editorialSessionStatus).font(.caption)
                    .foregroundStyle(model.editorialSessionStatus.contains("未保存") || model.editorialSessionStatus.contains("失败") ? .red : archiveBlue)
                Picker("项目类型", selection: $model.editorialTrack) {
                    Text("纪录片").tag("documentary")
                    Text("网感视频").tag("short_video")
                }.pickerStyle(.segmented)
                HStack {
                    Button("选择 PDF / TXT") { model.chooseEditorialFile() }
                    Button("添加逐句 Excel（可多选）") { model.chooseEditorialGuide() }
                }
                Button("选择包含文稿与 Excel 的文件夹") { model.chooseEditorialFolder() }
                Text(model.editorialSourceLabel).font(.caption).foregroundStyle(archiveMuted)
                Text(model.editorialGuideLabel).font(.caption).foregroundStyle(archiveBlue)
                if !model.editorialFolderFiles.isEmpty {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 4) {
                            ForEach(model.editorialFolderFiles, id: \.path) { file in
                                Button(file.lastPathComponent) { model.loadEditorialURL(file) }
                                    .buttonStyle(.link).lineLimit(1)
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.frame(maxHeight: 90)
                }
                if !model.editorialGuideFiles.isEmpty {
                    Button("清空指导，仅用主文稿") {
                        model.editorialGuideFiles = []; model.editorialGuideFile = ""
                        model.editorialGuideLabel = "未载入逐句指导；下次生成只使用主文稿"
                    }.font(.caption)
                }
                TextEditor(text: $model.editorialScript).font(.body)
                    .frame(minHeight: 300).padding(6)
                    .overlay(RoundedRectangle(cornerRadius: 7).stroke(Color.gray.opacity(0.3)))
                Text("PDF、TXT 或输入框决定真实口播句子；Excel 只是当前项目的逐句剪辑指导。也可拖入文件或文件夹。长句会按语意与标点拆成可剪辑句段。")
                    .font(.caption).foregroundStyle(archiveMuted)
                Button(model.editorialLoading ? "正在生成……" : "生成逐句候选") { model.generateEditorialBoard() }
                    .buttonStyle(PrimaryButtonStyle()).disabled(model.editorialLoading)
                Text(model.editorialStatus).font(.caption).foregroundStyle(model.editorialStatus.contains("失败") ? .red : archiveMuted)
            }.frame(maxWidth: .infinity, alignment: .leading)
          }
        }
    }

    private var candidatePanel: some View {
        Panel {
          ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("步骤 2 · 按表与系统补充，各最多 3 个").font(.headline)
                if let board = model.editorialBoard, !board.beats.isEmpty {
                    ScrollViewReader { proxy in
                      ScrollView(.horizontal, showsIndicators: true) {
                        LazyHStack(spacing: 6) {
                            ForEach(Array(board.beats.enumerated()), id: \.element.beatId) { index, beat in
                                Button("\(beat.order). \(beat.text.prefix(12))") { model.activateEditorialBeat(index) }
                                    .buttonStyle(.borderedProminent)
                                    .tint(index == model.editorialActiveBeat ? archiveBlue : Color.gray.opacity(0.5))
                                    .id(index)
                            }
                        }
                      }.frame(height: 34)
                       .onAppear { proxy.scrollTo(model.editorialActiveBeat, anchor: .center) }
                       .onChange(of: model.editorialActiveBeat) { index in proxy.scrollTo(index, anchor: .center) }
                    }
                    let index = min(model.editorialActiveBeat, board.beats.count - 1)
                    let beat = board.beats[index]
                    VStack(alignment: .leading, spacing: 6) {
                        Button("从我的收藏选画面 → 第 \(beat.order) 句") { model.openEditorialFavorites() }
                            .buttonStyle(.borderedProminent).tint(archiveBlue)
                        Button("去搜索素材 → 补选第 \(beat.order) 句") { model.startEditorialSearch() }
                            .disabled(model.searching || model.editorialSearchPending)
                    }.disabled(model.editorialLoading)
                    if let returnIndex = model.editorialReviewReturnIndex {
                        Button("返回第 \(returnIndex + 1) 句继续选片") { model.finishEditorialReview() }
                            .buttonStyle(.bordered).disabled(model.editorialLoading)
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        Text("第 \(beat.order) 句 · \(beat.purpose)").font(.caption).foregroundStyle(archiveBlue)
                        Text(beat.text).font(.title3.bold())
                        Text("前句：\(beat.contextBefore?.last ?? "开头")\n后句：\(beat.contextAfter?.first ?? "结尾")").font(.caption2).foregroundStyle(archiveMuted)
                        Text(String(format: "口播估时 %.1f 秒（未对齐真实录音）；镜头不足时可保留口播或补镜，不自动拉长素材。", Double(beat.estimatedNarrationMs ?? 0) / 1000)).font(.caption).foregroundStyle(archiveMuted)
                        EditorialDisclosure("本句画面需求与剪辑用途") {
                          VStack(alignment: .leading, spacing: 4) {
                          if let intent = beat.narrativeIntent, !intent.isEmpty {
                            Text(model.editorialText("叙事意图：\(intent) · 画面需求：\(beat.shotBrief ?? "待人工判断")"))
                                .font(.caption).foregroundStyle(archiveBlue)
                          }
                          Text(model.editorialText("画面任务：\(beat.visualTask ?? "UNKNOWN") · 主要主体：\(beat.expectedPrimarySubject ?? "UNKNOWN") · 推荐用途：\((beat.preferredShotRoles ?? []).joined(separator: " / "))"))
                            .font(.caption).foregroundStyle(archiveBlue)
                          Text("初筛涉及 \(beat.retrievalSourceCount) 个原文件，不代表全部适合。已用、已排除与本句已跳过的画面不会再次推荐。")
                            .font(.caption).foregroundStyle(archiveMuted)
                          }
                        }.font(.caption)
                    }
                    if let guidance = beat.projectEditorialGuidance {
                      EditorialDisclosure("查看当前项目逐句指导") {
                        VStack(alignment: .leading, spacing: 5) {
                            HStack {
                                Text("当前项目逐句指导").font(.caption.bold())
                                if let section = guidance.section, !section.isEmpty {
                                    Text(section).font(.caption2).foregroundStyle(archiveMuted)
                                }
                                Spacer()
                                if let confidence = guidance.matchConfidence {
                                    Text("匹配 \(Int((confidence * 100).rounded()))%")
                                        .font(.caption2).foregroundStyle(archiveBlue)
                                }
                            }
                            if let narration = guidance.guideNarration, !narration.isEmpty {
                                Text("表中对应：\(narration)").font(.caption2).foregroundStyle(archiveMuted)
                            }
                            if let direction = guidance.visualDirection, !direction.isEmpty {
                                Text("画面要求：\(direction)").font(.caption)
                            }
                            if let primary = guidance.primaryShot, !primary.isEmpty {
                                Text("主用建议：\(primary)").font(.caption)
                            }
                            if let alternative = guidance.alternativeShot, !alternative.isEmpty {
                                Text("补切/替换：\(alternative)").font(.caption)
                            }
                            if let editing = guidance.editingMethod, !editing.isEmpty {
                                Text("剪辑方法：\(editing)").font(.caption)
                            }
                            if let notes = guidance.notes, !notes.isEmpty {
                                Text("边界：\(notes)").font(.caption2).foregroundStyle(archiveOrange)
                            }
                            Text("按表组使用这里的画面要求和主用/替补日期；日期按目录名核对。长句可组合主选与补切，不强求一镜包办。未映射的G编号不会当作真实文件编号。")
                                .font(.caption2).foregroundStyle(archiveMuted)
                            Button("按本句指导重新找画面（保留已选）") { model.refreshEditorialBeat() }
                                .disabled(model.editorialLoading)
                            Button("带着画面要求去搜索素材 → 第 \(beat.order) 句") { model.startEditorialSearch() }
                                .disabled(model.editorialLoading || model.searching || model.editorialSearchPending)
                        }
                        .padding(9)
                        .background(Color.blue.opacity(0.06))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                      }.font(.caption)
                    } else {
                        Text(beat.guideMatchStatus == "NOT_LOADED"
                             ? "未加载逐句指导表：本句仅按口播和上下文分析。"
                             : "本句未可靠匹配到逐句表，未套用别句建议；这不是生成中断。可对照表中文稿检查改写或缺行。")
                            .font(.caption).foregroundStyle(archiveOrange)
                    }
                    if let guideSearch = beat.guideSearchMessage, !guideSearch.isEmpty {
                        EditorialDisclosure("查看日期/编号定位依据（未映射编号不算命中）") {
                            Text(guideSearch).font(.caption).foregroundStyle(archiveMuted)
                        }.font(.caption)
                    }
                    let candidates = model.editorialCandidates(for: index) + model.editorialCandidates(for: index, channel: "guide")
                    if candidates.isEmpty {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("素材缺口：当前没有通过门控的真实画面。")
                                .font(.caption.bold()).foregroundStyle(archiveOrange)
                            if let fallback = beat.fallbackPlan {
                                if let content = fallback.contentRequirement, !content.isEmpty {
                                    Text(model.editorialText("内容上需要：\(content)")).font(.caption)
                                }
                                if let aesthetic = fallback.aestheticRequirement, !aesthetic.isEmpty {
                                    Text("画面上需要：\(aesthetic)").font(.caption)
                                }
                                if let editing = fallback.editingResponsibility, !editing.isEmpty {
                                    Text(model.editorialText("剪辑上建议：\(editing)")).font(.caption)
                                }
                                if let capture = fallback.captureSuggestion, !capture.isEmpty {
                                    Text(model.editorialText("补拍建议：\(capture)")).font(.caption)
                                }
                            }
                            Text("可保留人物口播，或将上述要求作为补拍清单；缺口不占画面候选名额。")
                                .font(.caption2).foregroundStyle(archiveMuted)
                        }
                        .padding(9)
                        .background(Color.orange.opacity(0.07))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    } else {
                        VStack(spacing: 10) {
                            EditorialChannelSection(beat: beat, index: index, channel: "guide")
                            EditorialChannelSection(beat: beat, index: index, channel: "system")
                        }
                    }
                    if let aRoll = beat.aRollOption {
                        HStack(alignment: .center, spacing: 10) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("主叙述选择（不占画面候选名额）").font(.caption.bold())
                                Text(model.editorialText(aRoll.displayTitle)).font(.caption)
                                Text(model.editorialText(aRoll.fitReason)).font(.caption2).foregroundStyle(archiveMuted)
                            }
                            Spacer()
                            let active = model.editorialDecision(beat.beatId, aRoll.candidateId) == "selected"
                            Button(active ? "已选口播 · 点击取消" : "保留人物口播（A-roll）") {
                                model.setEditorialDecision(beat: beat, candidate: aRoll, decision: "selected")
                            }.buttonStyle(.borderedProminent).tint(active ? archiveGreen : archiveBlue)
                        }.padding(9).background(Color.blue.opacity(0.06)).clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    if let gap = beat.gapStatus {
                        Text("素材缺口选项（不占候选名额）：\(gap.reason)")
                            .font(.caption2).foregroundStyle(archiveMuted)
                    }
                    HStack {
                        Button("上一句") { model.activateEditorialBeat(max(0, index - 1)) }.disabled(index == 0)
                        Button("下一句") { model.activateEditorialBeat(min(board.beats.count - 1, index + 1)) }.disabled(index == board.beats.count - 1)
                        Spacer()
                        Text("当前素材库：\(editorialLibraryDisplayName(database: board.database, libraries: model.snapshot?.existingLibraries ?? []))")
                            .font(.caption2).foregroundStyle(archiveMuted).help(board.database)
                    }
                    Text(model.editorialRefreshStatus).font(.caption).foregroundStyle(archiveMuted)
                    Button("重新核对本句（保留选择和已跳过项）") { model.refreshEditorialBeat() }
                        .font(.caption)
                } else {
                    Spacer(); Text("尚未生成候选").foregroundStyle(archiveMuted).frame(maxWidth: .infinity); Spacer()
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
          }
        }
    }

    private var decisionPanel: some View {
        Panel {
            VStack(alignment: .leading, spacing: 12) {
                Text("步骤 3 · 候选箱").font(.headline)
                Text(model.editorialDecisionStatus).font(.caption).foregroundStyle(archiveBlue)
                    .lineLimit(2).help(model.editorialDecisionStatus)
                if let board = model.editorialBoard {
                    let saved = model.editorialSavedSelections()
                    let selected = saved.filter { model.editorialDecision($0.beat.beatId, $0.candidate.candidateId) == "selected" }
                    Text("已入选 \(selected.count) 个镜头 · 缺口 \(max(0, board.beats.count - Set(selected.map { $0.beat.beatId }).count)) 句")
                        .font(.caption).foregroundStyle(archiveMuted)
                    let backupCount = saved.filter { model.editorialDecision($0.beat.beatId, $0.candidate.candidateId) == "review" }.count
                    Text("备选 \(backupCount) 个 · 不代替主选，也不消除主剪缺口").font(.caption).foregroundStyle(archiveOrange)
                    // Native row reuse also bounds layout work while resizing;
                    // nested ForEach inside a ScrollView evaluated all beat groups.
                    let activeOrder = board.beats.indices.contains(model.editorialActiveBeat)
                        ? board.beats[model.editorialActiveBeat].order : 1
                    ScrollViewReader { proxy in
                      VStack(alignment: .leading, spacing: 6) {
                        Button("定位到第 \(activeOrder) 句附近") {
                            if let id = EditorialSelectionFocus.targetID(saved, activeOrder: activeOrder) {
                                proxy.scrollTo(id, anchor: .bottom)
                            }
                        }.font(.caption).disabled(saved.isEmpty)
                        List(saved) { row in
                            EditorialSelectionSummaryRow(selection: row) { editingSelection = row }
                                .id(row.id)
                        }.listStyle(.plain).environment(\.defaultMinListRowHeight, 112)
                         .buttonStyle(.borderless)
                         .onAppear {
                            DispatchQueue.main.async {
                                if let id = EditorialSelectionFocus.targetID(saved, activeOrder: activeOrder) {
                                    proxy.scrollTo(id, anchor: .bottom)
                                }
                            }
                         }
                         .onChange(of: activeOrder) { order in
                            if let id = EditorialSelectionFocus.targetID(saved, activeOrder: order) {
                                proxy.scrollTo(id, anchor: .bottom)
                            }
                         }
                         .onChange(of: saved.map(\.id)) { _ in
                            if let id = EditorialSelectionFocus.targetID(saved, activeOrder: activeOrder) {
                                proxy.scrollTo(id, anchor: .bottom)
                            }
                         }
                      }
                    }
                    .popover(item: $editingSelection, arrowEdge: .leading) { selection in
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Text("当前镜头 · 剪点与操作").font(.headline)
                                Spacer(); Button("完成") { editingSelection = nil }
                            }
                            if let current = model.editorialSavedSelections().first(where: { $0.id == selection.id }) {
                                EditorialSelectionRow(beat: current.beat, candidate: current.candidate)
                            } else {
                                Text("这条选择已取消；其他镜头和剪点未改变。").font(.caption)
                            }
                        }.padding(14).frame(width: 360).disabled(model.editorialLoading)
                    }
                } else { Text("候选生成后，这里显示人工选择。").font(.caption).foregroundStyle(archiveMuted) }
            }.frame(maxHeight: .infinity, alignment: .top)
        }
    }

    private var exportPanel: some View {
        Panel {
          ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                    Text("1 · 时间线设置").font(.headline)
                    TextField("时间线名称", text: $model.editorialTimelineName).textFieldStyle(.roundedBorder)
                    Picker("目标帧率", selection: $model.editorialFrameRate) {
                        Text("23.976").tag("24000/1001"); Text("24").tag("24")
                        Text("25").tag("25"); Text("29.97").tag("30000/1001")
                        Text("30").tag("30"); Text("48").tag("48"); Text("50").tag("50")
                        Text("59.94").tag("60000/1001"); Text("60").tag("60")
                        Text("100").tag("100"); Text("119.88").tag("120000/1001"); Text("120").tag("120")
                    }
                    Divider()
                    Text("2 · 包含哪些镜头").font(.headline)
                    Toggle("同时导出上层备选（默认禁用）", isOn: $model.editorialIncludeBackups).font(.caption)
                    Text("V1 放入选，V2 放同句备选（每句一条，默认禁用）。再次选择备选会替换本句旧备选。备选最长不超过本句主轨占位；导入达芬奇后请复核轨道。")
                        .font(.caption2).foregroundStyle(archiveMuted)
                    Text("未入选句会成为带文稿文字的时间线缺口，但不会占据推荐画面名额；FCPXML 引用原始文件，导出时必须挂载原素材硬盘。")
                        .font(.caption).foregroundStyle(archiveMuted)
                    Divider()
                    Text("3 · 导出文件").font(.headline)
                    HStack {
                        Button("导出 FCPXML 粗剪") { model.exportEditorialTimeline() }.buttonStyle(PrimaryButtonStyle())
                        Button("导出剪辑清单 JSON") { model.exportEditorialJSON() }.buttonStyle(.bordered)
                    }.disabled(model.editorialLoading)
                    Text(model.editorialExportStatus.isEmpty ? "尚未导出。请先核对时间线名称和目标帧率。" : model.editorialExportStatus)
                        .font(.caption).foregroundStyle(model.editorialExportStatus.contains("失败") ? .red : archiveMuted)
                        .textSelection(.enabled)
            }.frame(maxWidth: .infinity, alignment: .leading)
          }
        }
    }
}

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
                            Button("进入搜索素材") { model.openMainPage(.search) }.buttonStyle(PrimaryButtonStyle())
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

enum SearchDetailPanel: String { case evidence, annotations, person }
struct SearchDetailRequest: Identifiable {
    let result: SearchResult
    let panel: SearchDetailPanel
    var id: String { result.id + "::" + panel.rawValue }
}

// A constant-height browsing row. Detailed evidence/edit forms live in one
// page-owned sheet, never in every recycled table row. This also keeps an open
// editor alive when its original result scrolls offscreen.
struct SearchResultSummaryRow: View {
    @EnvironmentObject var model: ArchiveModel
    let result: SearchResult
    let inspect: (SearchDetailPanel) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 16) {
                EditorialThumbnail(path: result.previewPath ?? "", contentMode: .fill)
                    .frame(width: 220, height: 124).clipped().cornerRadius(8)
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Image(systemName: result.mediaType == "video" ? "video.fill" : "photo.fill")
                        Text(URL(fileURLWithPath: result.sourceRelativePath ?? "素材").lastPathComponent)
                            .font(.headline).lineLimit(1)
                        Spacer(minLength: 8)
                        Toggle("选入导出", isOn: Binding(
                            get: { model.isSelectedForExport(result) },
                            set: { model.setSelectedForExport(result, selected: $0) }
                        )).toggleStyle(.checkbox)
                    }
                    Text(result.mediaType == "video"
                         ? "片段 \(result.previewSegmentStartTimecode ?? "--") → \(result.previewSegmentEndTimecode ?? "--") · 命中 \(result.timecode ?? "--")"
                         : "图片素材").font(.caption).foregroundStyle(archiveBlue).lineLimit(1)
                    Text((result.audioTranscriptMatch == true ? "人声转写：" : "画面描述：") + (result.textPreview ?? "暂无文字描述，可查看已有画面。"))
                        .font(.subheadline).lineLimit(3).frame(height: 54, alignment: .topLeading)
                    Text("\(result.environmentLabel ?? "未标注") · 综合匹配 \(Int(((result.score ?? 0) * 100).rounded()))%（排序参考）")
                        .font(.caption2).foregroundStyle(archiveMuted).lineLimit(1)
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack(spacing: 10) {
                Button(result.mediaType == "video" ? "播放命中片段" : "打开图片") { model.open(result) }
                    .buttonStyle(.borderedProminent).disabled(result.canOpenOriginal != true)
                if result.mediaType == "video", let sourceId = result.sourceContentId {
                    Button("浏览全部画面") { model.browseSourceFrames(sourceId) }.buttonStyle(.bordered)
                }
                Button("详情 / 命中依据") { inspect(.evidence) }
                Button("收藏与备注") { inspect(.annotations) }
                Button("人物归类") { inspect(.person) }
                Button("Finder") { model.reveal(result) }.disabled(result.canOpenOriginal != true)
                Spacer(minLength: 0)
            }.font(.caption).frame(height: 28)
            if let beat = model.editorialSearchBeat {
                HStack {
                    Text("补选第 \(beat.order) 句：\(beat.text)").font(.caption).lineLimit(1)
                    Spacer(minLength: 8)
                    Button("入选第 \(beat.order) 句") { model.addSearchResultToEditorial(result, decision: "selected") }
                        .buttonStyle(.borderedProminent).tint(archiveGreen)
                    Button("备选第 \(beat.order) 句") { model.addSearchResultToEditorial(result, decision: "review") }
                        .buttonStyle(.bordered).tint(archiveOrange)
                    Button("回选片") { model.returnFromEditorialSearch() }
                }.font(.caption).frame(height: 28)
                 .disabled(model.editorialSearchPending || model.editorialLoading)
            }
            if model.editorialSearchTarget != nil {
                Text(model.editorialSearchMessages[result.exportSelectionId] ?? "播放核对后，可加入当前句；不会自动修改原选择。")
                    .font(.caption2).foregroundStyle(archiveBlue).lineLimit(2)
                    .frame(height: 30, alignment: .topLeading)
                    .help(model.editorialSearchMessages[result.exportSelectionId] ?? "")
            }
        // Each mode has a constant row height: no per-row expanding forms.
        }.padding(12).frame(height: model.editorialSearchTarget == nil ? 196 : 270)
         .frame(maxWidth: .infinity, alignment: .leading)
         .background(Color.white).cornerRadius(10)
    }
}

struct SearchResultCard: View {
    @EnvironmentObject var model: ArchiveModel; let result: SearchResult
    let initialPanel: SearchDetailPanel
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
    init(result: SearchResult, initialPanel: SearchDetailPanel = .evidence) {
        self.result = result
        self.initialPanel = initialPanel
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
            EditorialThumbnail(path: result.previewPath ?? "", contentMode: .fill)
                .frame(width: 250, height: 150).clipped().clipShape(RoundedRectangle(cornerRadius: 9))
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
                EditorialDisclosure("人工人物归类", initiallyExpanded: initialPanel == .person) {
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
                EditorialDisclosure("本地标签、备注与收藏", initiallyExpanded: initialPanel == .annotations) {
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
                if let beat = model.editorialSearchBeat {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("补选到第 \(beat.order) 句：\(beat.text)").font(.caption.bold()).lineLimit(2)
                        HStack {
                            Button("入选第 \(beat.order) 句") { model.addSearchResultToEditorial(result, decision: "selected") }
                                .buttonStyle(.borderedProminent).tint(archiveGreen)
                            Button("备选第 \(beat.order) 句") { model.addSearchResultToEditorial(result, decision: "review") }
                                .buttonStyle(.bordered).tint(archiveOrange)
                            Button("返回选片查看") { model.returnFromEditorialSearch() }.buttonStyle(.bordered)
                        }.disabled(model.editorialSearchPending || model.editorialLoading)
                        if let message = model.editorialSearchMessages[result.exportSelectionId] {
                            Text(message).font(.caption).foregroundStyle(archiveBlue)
                            Text(model.editorialSessionStatus).font(.caption2).foregroundStyle(archiveMuted)
                        }
                        Text("不用先收藏。加入的是此帧的临时剪点，回候选箱可播放、调整并锁定；不是自动判定适合。")
                            .font(.caption2).foregroundStyle(archiveMuted)
                    }.padding(10).background(Color.blue.opacity(0.05)).cornerRadius(8)
                }
            }
        }.padding(14).background(Color.white).clipShape(RoundedRectangle(cornerRadius: 12)).overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.black.opacity(0.07)))
    }
}

struct SearchPage: View {
    @EnvironmentObject var model: ArchiveModel
    @State private var detailRequest: SearchDetailRequest?
    // Native List owns scrolling/row reuse. A LazyVStack re-estimates hundreds
    // of variable-height cards while scrolling, shifting the scroll position.
    // Keep the controls in one measured header row, separate from result rows.
    var body: some View { List {
      VStack(alignment: .leading, spacing: 18) {
        PageHeader(title: "搜索素材", subtitle: "搜索当前选中的素材库；可在任务历史中切换到另一个已完成项目。")
        if let beat = model.editorialSearchBeat, let board = model.editorialBoard {
            Panel { VStack(alignment: .leading, spacing: 9) {
                HStack {
                    Text("正在为第 \(beat.order) 句补选").font(.headline)
                    Spacer()
                    Button("退出补选，普通搜索") { model.openMainPage(.search) }
                    Button("回第 \(beat.order) 句继续选片") { model.returnFromEditorialSearch() }
                        .disabled(model.editorialLoading)
                }
                Text(beat.text).font(.subheadline.bold())
                EditorialDisclosure("查看指导 / 更换补选句子") {
                    VStack(alignment: .leading, spacing: 8) {
                        Picker("加入哪一句", selection: Binding(get: { beat.beatId }, set: { model.bindEditorialSearchTarget($0) })) {
                            ForEach(board.beats) { row in Text("第 \(row.order) 句 · \(row.text.prefix(32))").tag(row.beatId) }
                        }.disabled(model.editorialSearchPending || model.editorialLoading)
                        Text("前句：\(beat.contextBefore?.last ?? "开头")\n后句：\(beat.contextAfter?.first ?? "结尾")")
                            .font(.caption).foregroundStyle(archiveMuted)
                        if let guide = beat.projectEditorialGuidance {
                            Text("画面要求：\(guide.visualDirection ?? "未填写")\n主用建议：\(guide.primaryShot ?? "未填写")")
                                .font(.caption).textSelection(.enabled)
                            Text("日期/地点是指导参考，尚未作为过滤条件。可在高级过滤填真实文件夹路径；日期过滤不是拍摄日期。")
                                .font(.caption2).foregroundStyle(archiveOrange)
                        }
                        Text("先播放命中片段或浏览全部画面，再点结果卡的入选/备选。切换补选句子只改变接收位置，不自动重搜。")
                            .font(.caption).foregroundStyle(archiveMuted)
                    }
                }
            } }
        } else if model.editorialSearchTarget != nil {
            Text("当前素材库与选片工程不一致或工程正在更新，暂不能补选；请连接原工程素材库。")
                .font(.caption).foregroundStyle(archiveOrange)
        }
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
            EditorialDisclosure("高级过滤（由数据库查询执行）") {
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
            EditorialDisclosure("当前素材库的搜索历史与保存查询") {
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
                EditorialDisclosure("本地人物名称、标签与人工合并") {
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
            Text(model.editorialSearchBeat != nil && !model.searchPrewarmReady
                 ? "本次只打开补选入口；点击搜索才开始查询。" : model.searchPrewarmStatus)
                .font(.caption).foregroundStyle(archiveMuted)
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
            EditorialDisclosure("展开诊断") {
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
      }.padding(.vertical, 20)
       .listRowInsets(EdgeInsets(top: 0, leading: 28, bottom: 0, trailing: 28))
        ForEach(model.searchResults) { result in
            SearchResultSummaryRow(result: result) { panel in
                detailRequest = SearchDetailRequest(result: result, panel: panel)
            }
                .listRowInsets(EdgeInsets(top: 7, leading: 28, bottom: 7, trailing: 28))
                .buttonStyle(.borderless)
        }
        if model.nextSearchOffset != nil {
            HStack { Spacer(); Button(model.searching ? "加载中…" : "加载更多") { model.search(loadMore: true) }.buttonStyle(PrimaryButtonStyle()).disabled(model.searching); Spacer() }.padding(.vertical, 12)
        }
    }.listStyle(.plain).sheet(item: $detailRequest) { request in
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("素材详情 · 收藏与人物编辑").font(.headline)
                Spacer()
                Button("完成 / 返回结果") { detailRequest = nil }.keyboardShortcut(.cancelAction)
            }
            ScrollView {
                SearchResultCard(result: request.result, initialPanel: request.panel)
            }
        }.padding(18).frame(width: 960, height: 620).environmentObject(model)
    }.onAppear {
        if model.personClusterCatalog.isEmpty { model.loadPersonClusters() }
        // Opening this manual fallback must not launch inference/prewarming.
        if model.editorialSearchTarget == nil { model.prewarmSearch() }
    } }
}

struct EditorialFavoritesPanel: View {
    @EnvironmentObject var model: ArchiveModel
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("从收藏补选 · 第 \(model.editorialActiveBeat + 1) 句").font(.title2.bold())
                Spacer()
                Button("完成 / 返回选片") { model.editorialFavoritesPresented = false; model.page = .editorial }
            }
            if let board = model.editorialBoard, board.beats.indices.contains(model.editorialActiveBeat) {
                Text(board.beats[model.editorialActiveBeat].text).font(.headline)
                Text("素材库：\(editorialLibraryDisplayName(database: board.database, libraries: model.snapshot?.existingLibraries ?? []))")
                    .font(.caption).foregroundStyle(archiveMuted)
                DisclosureGroup("查看数据库文件路径（用于核对，不是素材库名称）") {
                    Text(board.database).font(.caption2).textSelection(.enabled)
                }.font(.caption2)
            }
            Text(model.editorialFavoriteStatus).font(.caption).foregroundStyle(archiveBlue)
            HStack(alignment: .top) {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        Text("收藏原文件 \(model.editorialFavorites?.sources.count ?? 0) 个").font(.headline)
                        ForEach(model.editorialFavorites?.sources ?? []) { source in
                            Button { model.loadEditorialFavorites(sourceId: source.sourceContentId) } label: {
                                VStack(alignment: .leading, spacing: 3) {
                                    EditorialThumbnail(path: source.previewPath ?? "")
                                        .frame(width: 232, height: 130)
                                    Text(source.sourceFile).lineLimit(3)
                                    if let time = source.previewTimeMs {
                                        Text(String(format: "库内代表画面 · %.2f 秒", Double(time) / 1000)).font(.caption2)
                                    }
                                    if !source.note.isEmpty { Text(source.note).font(.caption).lineLimit(2) }
                                }.frame(maxWidth: .infinity, alignment: .leading).padding(7)
                                    .background(model.editorialFavoriteSourceId == source.id ? archiveBlue.opacity(0.12) : Color.clear)
                            }.buttonStyle(.plain)
                        }
                    }
                }.frame(width: 270)
                Divider()
                VStack(alignment: .leading) {
                    if model.editorialFavoritesLoading { ProgressView("读取已有画面…") }
                    else if model.editorialFavoriteSourceId.isEmpty { Text("先在左侧选择一个收藏原文件，再挑其中的具体画面。") }
                    else if model.editorialFavorites?.candidates.isEmpty != false { Text("该收藏暂无可用于当前选片流程的已索引画面，不会自动重新分析。") }
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            ForEach(model.editorialFavorites?.candidates ?? []) { candidate in
                                HStack(alignment: .top, spacing: 12) {
                                    EditorialThumbnail(path: candidate.previewPath).frame(width: 140, height: 88)
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(candidate.displayTitle).lineLimit(3)
                                        Text(String(format: "抽样点 %.2f 秒 · 临时范围 %.2f → %.2f 秒", Double(candidate.anchorTimeMs ?? 0)/1000, Double(candidate.provisionalInMs)/1000, Double(candidate.provisionalOutMs)/1000)).font(.caption)
                                        if let conflict = model.editorialFavoriteConflict(candidate) { Text(conflict).font(.caption).foregroundStyle(archiveOrange) }
                                        HStack {
                                            let choice = model.editorialBoard.flatMap { board in
                                                board.beats.indices.contains(model.editorialActiveBeat)
                                                    ? model.editorialDecision(board.beats[model.editorialActiveBeat].beatId, candidate.candidateId) : nil
                                            } ?? ""
                                            Button(choice == "selected" ? "已入选 · 再点取消" : "入选当前句") { model.chooseEditorialFavorite(candidate, decision: "selected") }
                                            Button(choice == "review" ? "已备选 · 再点取消" : "作为备选") { model.chooseEditorialFavorite(candidate, decision: "review") }
                                        }.disabled(model.editorialFavoritesLoading || model.editorialFavoriteConflict(candidate) != nil)
                                    }
                                }.padding(8).background(Color.white).clipShape(RoundedRectangle(cornerRadius: 7))
                            }
                        }
                    }.opacity(model.editorialFavoritesLoading ? 0.4 : 1)
                    if let page = model.editorialFavorites {
                        HStack {
                            Button("上一页画面") { model.loadEditorialFavorites(sourceId: model.editorialFavoriteSourceId, offset: max(0, page.offset - 9)) }.disabled(page.offset == 0)
                            Text("\(page.totalFrames) 个已有画面 · 本页最多9个").font(.caption)
                            Button("下一页画面") { if let next = page.nextOffset { model.loadEditorialFavorites(sourceId: model.editorialFavoriteSourceId, offset: next) } }.disabled(page.nextOffset == nil)
                        }.disabled(model.editorialFavoritesLoading)
                    }
                }
            }
            Text("左侧为库内代表画面：旧收藏只记录原文件，未保存收藏时的准确帧。点缩略图后在右侧挑具体画面；不修改收藏。入选后可播放、调整并锁定剪点，同一按钮再点可取消。")
                .font(.caption).foregroundStyle(archiveMuted)
        }.padding(22).frame(width: 980, height: 680)
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
                            Button("从收藏选画面 → 第 \(model.editorialActiveBeat + 1) 句") { model.openEditorialFavorites() }
                                .disabled(model.editorialBoard == nil || model.editorialLoading)
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
                        Text(model.editorialBoard == nil
                             ? "先到“文稿选片”打开或生成工程，再从这里为当前句挑选收藏画面。勾选“选入导出”只用于PDF/CSV，不是时间线入选。"
                             : "选片：点击“从收藏选画面”，选一个原文件，再选其中的画面并点入选/备选。勾选“选入导出”只用于PDF/CSV。")
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
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach(model.favoriteResults) { result in
                            VStack(alignment: .leading, spacing: 8) {
                                if let sourceId = result.sourceContentId, !sourceId.isEmpty, model.editorialBoard != nil {
                                    Button("选这条收藏中的画面 → 第 \(model.editorialActiveBeat + 1) 句") {
                                        model.openEditorialFavorites(sourceId: sourceId)
                                    }.disabled(model.editorialLoading)
                                }
                                SearchResultCard(result: result)
                            }
                        }
                    }
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
    @AppStorage("layout.sidebarVisible") private var sidebarVisible = true
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Button { sidebarVisible.toggle() } label: {
                    Image(systemName: "sidebar.left").frame(width: 30, height: 28)
                }.buttonStyle(.bordered)
                 .accessibilityLabel(sidebarVisible ? "隐藏左侧导航栏" : "显示左侧导航栏")
                 .help(sidebarVisible ? "隐藏左侧导航栏（Control-Command-S）" : "显示左侧导航栏（Control-Command-S）")
                 .keyboardShortcut("s", modifiers: [.control, .command])
                if model.page == .editorial && model.editorialBoard != nil {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("选片工程 · 第 \(model.editorialActiveBeat + 1) 句").font(.headline)
                        Text(model.editorialSessionStatus).font(.caption)
                            .foregroundStyle(model.editorialSessionStatus.contains("失败") || model.editorialSessionStatus.contains("未保存") ? .red : archiveBlue)
                            .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                            .help(model.editorialSessionStatus)
                    }.layoutPriority(1)
                    Spacer()
                    Button("保存进度") { model.autosaveEditorialSession() }.keyboardShortcut("s", modifiers: .command)
                        .disabled(model.editorialLoading)
                    Menu("工程操作") {
                        Button("另存工程副本…") { model.saveEditorialProjectCopy() }
                        Button("打开工程…") { model.openEditorialProject() }
                    }.frame(width: 110).disabled(model.editorialLoading)
                } else {
                    Text(model.page.rawValue).font(.headline)
                    Spacer()
                }
            }.padding(.horizontal, 14).padding(.vertical, 9)
             .fixedSize(horizontal: false, vertical: true).layoutPriority(1)
             .background(Color.white)
            Divider()
            HStack(spacing: 0) {
              if sidebarVisible { Sidebar(); Divider() }
              Group {
                switch model.page {
                case .newTask: NewTaskPage()
                case .running: RunningPage()
                case .history: HistoryPage()
                case .search: SearchPage()
                case .favorites: FavoritesPage()
                case .editorial: EditorialPage()
                case .reference: EditorialReferencePage()
                case .duplicates: DuplicatesPage()
                case .special: SpecialPage()
                case .settings: SettingsPage()
                }
              }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }.background(archiveBackground).frame(minWidth: 1000, minHeight: 680)
            .sheet(isPresented: $model.editorialFavoritesPresented) { EditorialFavoritesPanel().environmentObject(model) }
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

final class ArchiveApplicationDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    var model: ArchiveModel?
    private var awaitingSave = false
    func windowWillClose(_ notification: Notification) { model?.closePreviewWindows() }
    func windowDidMiniaturize(_ notification: Notification) { model?.pausePreviewWindows() }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model else { return .terminateNow }
        guard !awaitingSave else { return .terminateLater }
        awaitingSave = true
        model.finishEditorialSaves { result in
            self.awaitingSave = false
            switch result {
            case .success: sender.reply(toApplicationShouldTerminate: true)
            case .failure(let error):
                let alert = NSAlert(); alert.messageText = "工程尚未保存，已取消退出"
                alert.informativeText = error.localizedDescription + "\n请先保存工程副本或重试保存进度。"
                alert.runModal(); sender.reply(toApplicationShouldTerminate: false)
            }
        }
        return .terminateLater
    }
}

if CommandLine.arguments.contains("--check") { runCheckAndExit() }
let application = NSApplication.shared
application.setActivationPolicy(.regular)
let model = ArchiveModel()
let appDelegate = ArchiveApplicationDelegate(); appDelegate.model = model
application.delegate = appDelegate
// A native menu is required for Cmd-Q to reach the save-aware delegate.
let mainMenu = NSMenu()
let appMenuItem = NSMenuItem()
let appMenu = NSMenu(title: bundledAppName)
appMenu.addItem(withTitle: "关于 \(bundledAppName)", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
appMenu.addItem(.separator())
appMenu.addItem(withTitle: "退出 \(bundledAppName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
appMenuItem.submenu = appMenu
mainMenu.addItem(appMenuItem)
let editMenuItem = NSMenuItem()
let editMenu = NSMenu(title: "编辑")
editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
editMenuItem.submenu = editMenu
mainMenu.addItem(editMenuItem)
application.mainMenu = mainMenu
// Standard titlebar reserves its own space; project status is never laid out
// underneath traffic lights/titlebar at compact window sizes.
let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1460, height: 900), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
window.delegate = appDelegate
window.title = bundledAppName; window.titlebarAppearsTransparent = true
window.contentView = NSHostingView(rootView: RootView().environmentObject(model))
window.center(); window.makeKeyAndOrderFront(nil)
application.activate(ignoringOtherApps: true)
application.run()
