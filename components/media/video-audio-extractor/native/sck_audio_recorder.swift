import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

enum RecorderError: Error, CustomStringConvertible {
    case missingArgument(String)
    case noDisplay
    case noAudioSamples
    case writerFailed(String)

    var description: String {
        switch self {
        case .missingArgument(let name):
            return "missing argument: \(name)"
        case .noDisplay:
            return "no display available for ScreenCaptureKit capture"
        case .noAudioSamples:
            return "no system audio samples were captured; check Screen Recording/System Audio permissions and playback"
        case .writerFailed(let message):
            return "asset writer failed: \(message)"
        }
    }
}

@available(macOS 13.0, *)
final class SystemAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let outputURL: URL
    private let writer: AVAssetWriter
    private let audioInput: AVAssetWriterInput
    private let sampleQueue = DispatchQueue(label: "local.sck-audio-recorder.samples")
    private let lock = NSLock()
    private var stream: SCStream?
    private var didStartWriting = false
    private var sampleCount = 0

    init(outputURL: URL, bitRate: Int) throws {
        self.outputURL = outputURL
        self.writer = try AVAssetWriter(outputURL: outputURL, fileType: .m4a)
        self.audioInput = AVAssetWriterInput(
            mediaType: .audio,
            outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVNumberOfChannelsKey: 2,
                AVSampleRateKey: 48_000,
                AVEncoderBitRateKey: bitRate,
            ]
        )
        self.audioInput.expectsMediaDataInRealTime = true
        super.init()
        guard writer.canAdd(audioInput) else {
            throw RecorderError.writerFailed("cannot add audio writer input")
        }
        writer.add(audioInput)
    }

    func record(duration: Double) async throws -> [String: Any] {
        if FileManager.default.fileExists(atPath: outputURL.path) {
            try FileManager.default.removeItem(at: outputURL)
        }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw RecorderError.noDisplay
        }

        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 3
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = 48_000
        configuration.channelCount = 2

        let stream = SCStream(filter: filter, configuration: configuration, delegate: self)
        self.stream = stream
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: sampleQueue)
        try await stream.startCapture()
        try await Task.sleep(nanoseconds: UInt64(max(duration, 0.1) * 1_000_000_000))
        try await stream.stopCapture()

        sampleQueue.sync {}
        try await finishWriting()
        if sampleCount == 0 {
            throw RecorderError.noAudioSamples
        }
        let attrs = try FileManager.default.attributesOfItem(atPath: outputURL.path)
        return [
            "output": outputURL.path,
            "bytes": attrs[.size] as? UInt64 ?? 0,
            "samples": sampleCount,
            "duration_seconds": duration,
        ]
    }

    nonisolated func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, sampleBuffer.isValid, CMSampleBufferDataIsReady(sampleBuffer) else {
            return
        }
        lock.lock()
        defer { lock.unlock() }
        if !didStartWriting {
            guard writer.startWriting() else {
                return
            }
            writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
            didStartWriting = true
        }
        guard audioInput.isReadyForMoreMediaData else {
            return
        }
        if audioInput.append(sampleBuffer) {
            sampleCount += 1
        }
    }

    private func finishWriting() async throws {
        if !didStartWriting {
            writer.cancelWriting()
            return
        }
        audioInput.markAsFinished()
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            writer.finishWriting {
                if self.writer.status == .failed {
                    continuation.resume(
                        throwing: RecorderError.writerFailed(self.writer.error?.localizedDescription ?? "unknown")
                    )
                } else {
                    continuation.resume()
                }
            }
        }
    }
}

func argumentValue(_ name: String, in args: [String]) -> String? {
    guard let index = args.firstIndex(of: name), index + 1 < args.count else {
        return nil
    }
    return args[index + 1]
}

@main
struct Main {
    static func main() async {
        do {
            guard #available(macOS 13.0, *) else {
                throw RecorderError.writerFailed("ScreenCaptureKit audio capture requires macOS 13 or newer")
            }
            let args = CommandLine.arguments
            guard let output = argumentValue("--output", in: args) else {
                throw RecorderError.missingArgument("--output")
            }
            guard let durationText = argumentValue("--duration", in: args), let duration = Double(durationText) else {
                throw RecorderError.missingArgument("--duration")
            }
            let bitRate = Int(argumentValue("--bitrate", in: args) ?? "128000") ?? 128_000
            let recorder = try SystemAudioRecorder(outputURL: URL(fileURLWithPath: output), bitRate: bitRate)
            let result = try await recorder.record(duration: duration)
            let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write("\n".data(using: .utf8)!)
        } catch {
            let payload: [String: Any] = ["error": String(describing: error)]
            if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys]) {
                FileHandle.standardError.write(data)
                FileHandle.standardError.write("\n".data(using: .utf8)!)
            } else {
                FileHandle.standardError.write("\(error)\n".data(using: .utf8)!)
            }
            exit(2)
        }
    }
}
