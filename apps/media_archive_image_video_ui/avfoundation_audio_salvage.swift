import Foundation
import AVFoundation

enum SalvageError: Error, CustomStringConvertible {
    case usage
    case noAudioTrack
    case readerUnavailable(String)
    case readerFailed(String)
    case emptyOutput

    var description: String {
        switch self {
        case .usage:
            return "usage: AVFoundationAudioSalvage INPUT OUTPUT_WAV [DURATION_SECONDS]"
        case .noAudioTrack:
            return "source has no audio track"
        case .readerUnavailable(let detail):
            return "AVFoundation cannot create an audio reader: \(detail)"
        case .readerFailed(let detail):
            return "AVFoundation audio decode failed: \(detail)"
        case .emptyOutput:
            return "AVFoundation produced no PCM audio"
        }
    }
}

func appendLittleEndian<T: FixedWidthInteger>(_ value: T, to data: inout Data) {
    var littleEndian = value.littleEndian
    withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
}

func writeMonoPCM16WAV(_ pcm: Data, sampleRate: UInt32, output: URL) throws {
    guard !pcm.isEmpty, pcm.count <= Int(UInt32.max) - 36 else {
        throw SalvageError.emptyOutput
    }
    var wav = Data("RIFF".utf8)
    appendLittleEndian(UInt32(36 + pcm.count), to: &wav)
    wav.append(Data("WAVEfmt ".utf8))
    appendLittleEndian(UInt32(16), to: &wav)
    appendLittleEndian(UInt16(1), to: &wav)
    appendLittleEndian(UInt16(1), to: &wav)
    appendLittleEndian(sampleRate, to: &wav)
    appendLittleEndian(sampleRate * 2, to: &wav)
    appendLittleEndian(UInt16(2), to: &wav)
    appendLittleEndian(UInt16(16), to: &wav)
    wav.append(Data("data".utf8))
    appendLittleEndian(UInt32(pcm.count), to: &wav)
    wav.append(pcm)
    try wav.write(to: output, options: .atomic)
}

func extractAudio(arguments: [String]) throws {
    guard arguments.count == 3 || arguments.count == 4 else {
        throw SalvageError.usage
    }
    let input = URL(fileURLWithPath: arguments[1])
    let output = URL(fileURLWithPath: arguments[2])
    let asset = AVURLAsset(url: input)
    guard let track = asset.tracks(withMediaType: .audio).first else {
        throw SalvageError.noAudioTrack
    }
    let reader: AVAssetReader
    do {
        reader = try AVAssetReader(asset: asset)
    } catch {
        throw SalvageError.readerUnavailable(String(describing: error))
    }
    if arguments.count == 4, let seconds = Double(arguments[3]), seconds > 0 {
        reader.timeRange = CMTimeRange(
            start: .zero,
            duration: CMTime(seconds: seconds, preferredTimescale: 600)
        )
    }
    let sampleRate: UInt32 = 16_000
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: sampleRate,
        AVNumberOfChannelsKey: 1,
        AVLinearPCMBitDepthKey: 16,
        AVLinearPCMIsFloatKey: false,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: false,
    ]
    let trackOutput = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
    trackOutput.alwaysCopiesSampleData = false
    guard reader.canAdd(trackOutput) else {
        throw SalvageError.readerUnavailable("cannot add PCM track output")
    }
    reader.add(trackOutput)
    guard reader.startReading() else {
        throw SalvageError.readerFailed(reader.error.map(String.init(describing:)) ?? "startReading failed")
    }

    var pcm = Data()
    while let sample = trackOutput.copyNextSampleBuffer() {
        guard let block = CMSampleBufferGetDataBuffer(sample) else { continue }
        let length = CMBlockBufferGetDataLength(block)
        if length <= 0 { continue }
        var bytes = [UInt8](repeating: 0, count: length)
        let status = CMBlockBufferCopyDataBytes(
            block, atOffset: 0, dataLength: length, destination: &bytes
        )
        guard status == kCMBlockBufferNoErr else {
            throw SalvageError.readerFailed("CMBlockBufferCopyDataBytes=\(status)")
        }
        pcm.append(contentsOf: bytes)
    }
    guard reader.status == .completed else {
        throw SalvageError.readerFailed(
            reader.error.map(String.init(describing:)) ?? "status=\(reader.status.rawValue)"
        )
    }
    try writeMonoPCM16WAV(pcm, sampleRate: sampleRate, output: output)
}

do {
    try extractAudio(arguments: CommandLine.arguments)
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
