using System.Reflection;
using System.Runtime.InteropServices;
using BwReader.ComputerVoiceAudio.Interop;

namespace BwReader.ComputerVoiceAudio;

internal static class ContractSelfTest
{
    internal static object Run()
    {
        List<string> checks = [];

        CheckActivationContract(checks);
        CheckExplicitMicrophoneContract(checks);
        CheckInteropLayout(checks);
        CheckBoundedPacketPump(checks);
        CheckPcm48kMonoFramer(checks);
        CheckCaptureThreadAffinity(checks);
        CheckSessionLifecycle(checks);
        CheckExplicitMicrophoneLifecycle(checks);
        CheckVirtualMicrophoneRenderContract(checks);
        CheckCaptureEndpointMuteLease(checks);
        CheckInteropVtables(checks);
        PerAppAudioRouteSelfTest.Run(checks);
        CodexVoiceActivitySelfTest.Run(checks);
        CodexVoiceHistorySelfTest.Run(checks);
        DirectBridgeSelfTest.Run(checks);
        ReaderAttentionBoardSelfTest.Run(checks);
        DisconnectCleanupWatchdogSelfTest.Run(checks);

        return new
        {
            contract = AudioBridgeContract.Contract,
            ok = true,
            audioActivated = false,
            checks,
        };
    }

    private static void CheckCaptureEndpointMuteLease(
        ICollection<string> checks)
    {
        const string endpointId = "capture-endpoint-A";
        FakeCaptureEndpointMuteBackend muted = new(
            initialMuted: true);
        DirectCaptureEndpointMuteLease lease =
            DirectCaptureEndpointMuteLease.Acquire(
                muted,
                endpointId);
        lease.RequireUnmuted();
        Require(
            !muted.Muted
            && muted.Writes.SequenceEqual([false])
            && muted.EndpointIds.All(value => value == endpointId),
            "capture-endpoint-muted-is-unmuted-by-exact-id",
            checks);
        lease.Restore();
        lease.Restore();
        Require(
            muted.Muted
            && muted.Writes.SequenceEqual([false, true]),
            "capture-endpoint-original-mute-restored-once",
            checks);

        FakeCaptureEndpointMuteBackend alreadyUnmuted = new(
            initialMuted: false);
        DirectCaptureEndpointMuteLease noChange =
            DirectCaptureEndpointMuteLease.Acquire(
                alreadyUnmuted,
                endpointId);
        noChange.RequireUnmuted();
        noChange.Restore();
        Require(
            !alreadyUnmuted.Muted
            && alreadyUnmuted.Writes.Count == 0,
            "capture-endpoint-existing-unmuted-state-is-left-alone",
            checks);

        FakeCaptureEndpointMuteBackend remuted = new(
            initialMuted: true);
        DirectCaptureEndpointMuteLease remutedLease =
            DirectCaptureEndpointMuteLease.Acquire(
                remuted,
                endpointId);
        remuted.Muted = true;
        bool remuteRejected = false;
        try
        {
            remutedLease.RequireUnmuted();
        }
        catch (DirectProtocolException exception)
            when (
                exception.Code
                    == "BW_COMPUTER_VOICE_DIRECT_MIC_ENDPOINT_MUTED"
            )
        {
            remuteRejected = true;
        }
        Require(
            remuteRejected,
            "capture-endpoint-remute-fails-before-shortcut",
            checks);
        remutedLease.Restore();

        FakeCaptureEndpointMuteBackend stuckMuted = new(
            initialMuted: true,
            ignoreUnmute: true);
        bool readbackRejected = false;
        try
        {
            _ = DirectCaptureEndpointMuteLease.Acquire(
                stuckMuted,
                endpointId);
        }
        catch (DirectProtocolException exception)
            when (
                exception.Code
                    == "BW_COMPUTER_VOICE_DIRECT_MIC_ENDPOINT_UNMUTE_FAILED"
            )
        {
            readbackRejected = true;
        }
        Require(
            readbackRejected && stuckMuted.Muted,
            "capture-endpoint-unmute-readback-fails-closed",
            checks);
    }

    private static void CheckPcm48kMonoFramer(
        ICollection<string> checks)
    {
        PcmAudioFormat stereo48k = new(
            PcmSampleEncoding.IntegerPcm,
            FormatTag: PcmAudioFormat.WaveFormatPcm,
            Channels: 2,
            SamplesPerSecond: 48_000,
            AverageBytesPerSecond: 192_000,
            BlockAlign: 4,
            BitsPerSample: 16,
            ValidBitsPerSample: 16,
            ExtraSize: 0,
            ChannelMask: 0,
            SubFormat: PcmAudioFormat.SubtypePcm);
        byte[] source = new byte[Pcm48kMonoFramer.FramesPerChunk * 4];
        for (int frame = 0; frame < Pcm48kMonoFramer.FramesPerChunk; frame++)
        {
            System.Buffers.Binary.BinaryPrimitives.WriteInt16LittleEndian(
                source.AsSpan(frame * 4, 2),
                short.MaxValue);
            System.Buffers.Binary.BinaryPrimitives.WriteInt16LittleEndian(
                source.AsSpan(frame * 4 + 2, 2),
                0);
        }
        Pcm48kMonoFramer framer = new(stereo48k);
        framer.Push(new PcmPacket(
            source,
            Pcm48kMonoFramer.FramesPerChunk,
            Silent: false,
            Discontinuous: false,
            TimestampError: false,
            DevicePosition: 0,
            QpcPosition: 0));
        bool framed = framer.TryRead(out PcmFrameChunk chunk);
        short first = framed
            ? System.Buffers.Binary.BinaryPrimitives.ReadInt16LittleEndian(
                chunk.Data.AsSpan(0, 2))
            : (short)0;
        Require(
            framed
            && chunk.Sequence == 0
            && chunk.TimestampUs == 0
            && chunk.Data.Length == Pcm48kMonoFramer.BytesPerChunk
            && first is >= 16382 and <= 16384,
            "pcm-framer-stereo-to-fixed-mono-s16",
            checks);

        bool unsupportedRateRejected = false;
        try
        {
            _ = new Pcm48kMonoFramer(TestFormat());
        }
        catch (InvalidOperationException exception)
            when (exception.Message.Contains(
                "BW_COMPUTER_VOICE_AUDIO_SAMPLE_RATE_UNSUPPORTED",
                StringComparison.Ordinal))
        {
            unsupportedRateRejected = true;
        }
        Require(
            unsupportedRateRejected,
            "pcm-framer-unsupported-rate-fails-closed",
            checks);
    }

    private static void CheckActivationContract(ICollection<string> checks)
    {
        Require(
            (int)AudioClientActivationType.ProcessLoopback == 1,
            "activation-type-process-loopback",
            checks);
        Require(
            (int)ProcessLoopbackMode.IncludeTargetProcessTree == 0,
            "include-target-process-tree",
            checks);
        Require(
            ProcessLoopbackInterop.VirtualAudioDeviceProcessLoopback
                == "VAD\\Process_Loopback",
            "virtual-process-loopback-device",
            checks);
        Require(
            (uint)AudioClientStreamFlags.Loopback == 0x0002_0000
            && (uint)AudioClientStreamFlags.EventCallback == 0x0004_0000
            && (uint)AudioClientStreamFlags.AutoConvertPcm == 0x8000_0000,
            "shared-event-loopback-flags",
            checks);
        AudioClientStreamFlags processFlags =
            NativeProcessLoopbackCaptureRuntime.StreamFlagsForTest;
        WaveFormatEx processFormat =
            NativeProcessLoopbackCaptureRuntime.CaptureFormatForTest;
        Require(
            processFlags
                == (
                    AudioClientStreamFlags.Loopback
                    | AudioClientStreamFlags.EventCallback
                    | AudioClientStreamFlags.AutoConvertPcm)
            && processFormat.FormatTag == PcmAudioFormat.WaveFormatPcm
            && processFormat.Channels == 2
            && processFormat.SamplesPerSecond
                == Pcm48kMonoFramer.SampleRate
            && processFormat.AverageBytesPerSecond == 192_000
            && processFormat.BlockAlign == 4
            && processFormat.BitsPerSample == 16
            && processFormat.ExtraSize == 0,
            "process-loopback-fixed-pcm48-autoconvert",
            checks);
        Require(
            (uint)AudioClientBufferFlags.Silent == 0x2,
            "silent-buffer-flag",
            checks);

        const uint sampleProcessId = 31180;
        AudioClientActivationParams parameters =
            ProcessLoopbackActivation.BuildParameters(sampleProcessId);
        Require(
            parameters.ActivationType == AudioClientActivationType.ProcessLoopback,
            "builder-process-loopback-only",
            checks);
        Require(
            parameters.ProcessLoopbackParams.TargetProcessId == sampleProcessId,
            "builder-target-pid",
            checks);
        Require(
            parameters.ProcessLoopbackParams.ProcessLoopbackMode
                == ProcessLoopbackMode.IncludeTargetProcessTree,
            "builder-includes-target-tree",
            checks);
        Require(
            ProcessLoopbackActivation
                .NativeCompletionHandlerIsAgileForTest(),
            "native-completion-handler-is-agile",
            checks);

        bool zeroPidRejected = false;
        try
        {
            _ = ProcessLoopbackActivation.BuildParameters(0);
        }
        catch (ArgumentOutOfRangeException exception)
            when (exception.Message.Contains(
                "BW_COMPUTER_VOICE_AUDIO_TARGET_PID_REQUIRED",
                StringComparison.Ordinal))
        {
            zeroPidRejected = true;
        }

        Require(zeroPidRejected, "zero-pid-fails-closed", checks);
    }

    private static void CheckExplicitMicrophoneContract(
        ICollection<string> checks)
    {
        const string exactEndpointId =
            "{0.0.1.00000000}.{A1B2C3D4-E5F6-47A8-9012-3456789ABCDE}";
        MicCaptureRequest request = MicCaptureRequest.Create(exactEndpointId);
        Require(
            request.EndpointId == exactEndpointId,
            "mic-endpoint-id-preserved-exactly",
            checks);

        string?[] invalidIds =
        [
            null,
            "",
            "   ",
            "endpoint\u0000id",
            "endpoint\nid",
            new string('x', MicCaptureRequest.MaximumEndpointIdLength + 1),
        ];
        bool invalidIdsRejected = invalidIds.All(endpointId =>
        {
            try
            {
                _ = MicCaptureRequest.Create(endpointId);
                return false;
            }
            catch (ArgumentException)
            {
                return true;
            }
        });
        Require(
            invalidIdsRejected,
            "mic-invalid-endpoint-ids-fail-closed",
            checks);

        MethodInfo? forbiddenEnumeration =
            typeof(IMMDeviceEnumerator).GetMethod(
                "ForbiddenEnumAudioEndpoints");
        MethodInfo? forbiddenDefault =
            typeof(IMMDeviceEnumerator).GetMethod(
                "ForbiddenGetDefaultAudioEndpoint");
        Require(
            forbiddenEnumeration
                ?.GetCustomAttribute<ObsoleteAttribute>()?.IsError == true
            && forbiddenDefault
                ?.GetCustomAttribute<ObsoleteAttribute>()?.IsError == true,
            "mic-default-and-enumeration-slots-compile-time-forbidden",
            checks);

        string[] resolverMethods =
            typeof(IExplicitMicrophoneAudioClientLeaseFactory)
                .GetMethods()
                .Select(method => method.Name)
                .ToArray();
        Require(
            resolverMethods.SequenceEqual(new[] { "OpenExact" }),
            "mic-resolver-exposes-explicit-id-only",
            checks);

        FieldInfo? sharedRuntime =
            typeof(ExplicitMicrophoneCaptureRuntime).GetField(
                "_inner",
                BindingFlags.Instance | BindingFlags.NonPublic);
        Require(
            sharedRuntime?.FieldType
                == typeof(SharedEventDrivenPcmRuntime),
            "mic-reuses-shared-event-pcm-runtime",
            checks);

        FakeNativeAudioClientLease microphoneLease = new();
        bool microphoneUsesDeviceMixFormat;
        using (ExplicitMicrophoneCaptureRuntime microphoneRuntime =
            new(microphoneLease))
        {
            object? inner = sharedRuntime?.GetValue(microphoneRuntime);
            FieldInfo? fixedCaptureFormat =
                typeof(SharedEventDrivenPcmRuntime).GetField(
                    "_fixedCaptureFormat",
                    BindingFlags.Instance | BindingFlags.NonPublic);
            microphoneUsesDeviceMixFormat =
                inner is not null
                && fixedCaptureFormat?.GetValue(inner) is null;
        }
        Require(
            microphoneUsesDeviceMixFormat
            && microphoneLease.DisposeCount == 1,
            "mic-shared-runtime-keeps-device-mix-format",
            checks);
    }

    private static void CheckInteropLayout(ICollection<string> checks)
    {
        Require(
            ProcessLoopbackInterop.VariantTypeBlob == 65,
            "propvariant-vt-blob",
            checks);
        Require(
            Marshal.SizeOf<AudioClientProcessLoopbackParams>() == 8,
            "process-params-layout",
            checks);
        Require(
            Marshal.SizeOf<AudioClientActivationParams>() == 12,
            "activation-params-layout",
            checks);
        Require(
            Marshal.SizeOf<PropVariant>() == (IntPtr.Size == 8 ? 24 : 16),
            "propvariant-layout",
            checks);
        Require(
            Marshal.SizeOf<WaveFormatEx>() == 18,
            "waveformatex-layout",
            checks);
        Require(
            Marshal.SizeOf<WaveFormatExtensible>() == 40,
            "waveformatextensible-layout",
            checks);

        PcmAudioFormat format = TestFormat();
        format.Validate();
        Require(
            format.BlockAlign == 2
            && format.SamplesPerSecond == 8000
            && format.AverageBytesPerSecond == 16000,
            "pcm-format-validation",
            checks);

        bool invalidFormatRejected = false;
        try
        {
            new PcmAudioFormat(
                PcmSampleEncoding.IntegerPcm,
                PcmAudioFormat.WaveFormatPcm,
                1,
                8000,
                1,
                2,
                16,
                16,
                0,
                0,
                PcmAudioFormat.SubtypePcm).Validate();
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_MIX_FORMAT_RATE_INVALID")
        {
            invalidFormatRejected = true;
        }

        Require(
            invalidFormatRejected,
            "invalid-mix-format-fails-closed",
            checks);

        WaveFormatEx integerPcm = new()
        {
            FormatTag = PcmAudioFormat.WaveFormatPcm,
            Channels = 2,
            SamplesPerSecond = 48000,
            AverageBytesPerSecond = 192000,
            BlockAlign = 4,
            BitsPerSample = 16,
            ExtraSize = 0,
        };
        PcmAudioFormat parsedInteger = ParseNativeFormat(integerPcm);
        Require(
            parsedInteger.Encoding == PcmSampleEncoding.IntegerPcm
            && parsedInteger.SubFormat == PcmAudioFormat.SubtypePcm,
            "parse-integer-pcm-format",
            checks);

        WaveFormatEx ieeeFloat = new()
        {
            FormatTag = PcmAudioFormat.WaveFormatIeeeFloat,
            Channels = 2,
            SamplesPerSecond = 48000,
            AverageBytesPerSecond = 384000,
            BlockAlign = 8,
            BitsPerSample = 32,
            ExtraSize = 0,
        };
        PcmAudioFormat parsedFloat = ParseNativeFormat(ieeeFloat);
        Require(
            parsedFloat.Encoding == PcmSampleEncoding.IeeeFloat
            && parsedFloat.SubFormat == PcmAudioFormat.SubtypeIeeeFloat,
            "parse-ieee-float-format",
            checks);

        WaveFormatExtensible extensibleFloat = new()
        {
            Format = new WaveFormatEx
            {
                FormatTag = PcmAudioFormat.WaveFormatExtensible,
                Channels = 2,
                SamplesPerSecond = 48000,
                AverageBytesPerSecond = 384000,
                BlockAlign = 8,
                BitsPerSample = 32,
                ExtraSize = 22,
            },
            ValidBitsPerSample = 32,
            ChannelMask = 3,
            SubFormat = PcmAudioFormat.SubtypeIeeeFloat,
        };
        PcmAudioFormat parsedExtensible = ParseNativeFormat(extensibleFloat);
        Require(
            parsedExtensible.Encoding == PcmSampleEncoding.IeeeFloat
            && parsedExtensible.ValidBitsPerSample == 32
            && parsedExtensible.ChannelMask == 3,
            "parse-extensible-float-format",
            checks);

        bool compressedRejected = false;
        try
        {
            _ = ParseNativeFormat(new WaveFormatEx
            {
                FormatTag = 0x0055,
                Channels = 2,
                SamplesPerSecond = 48000,
                AverageBytesPerSecond = 16000,
                BlockAlign = 1,
                BitsPerSample = 8,
                ExtraSize = 12,
            });
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_MIX_FORMAT_UNSUPPORTED")
        {
            compressedRejected = true;
        }

        Require(
            compressedRejected,
            "compressed-format-fails-closed",
            checks);

        bool unknownSubtypeRejected = false;
        try
        {
            extensibleFloat.SubFormat = Guid.NewGuid();
            _ = ParseNativeFormat(extensibleFloat);
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_MIX_SUBFORMAT_UNSUPPORTED")
        {
            unknownSubtypeRejected = true;
        }

        Require(
            unknownSubtypeRejected,
            "unknown-extensible-subformat-fails-closed",
            checks);
    }

    private static void CheckBoundedPacketPump(ICollection<string> checks)
    {
        CaptureSessionOptions options = new()
        {
            MaximumPacketBytes = 32,
            MaximumPacketsPerWake = 4,
        };
        PcmAudioFormat format = TestFormat();

        nint samplePointer = Marshal.AllocHGlobal(4);
        try
        {
            Marshal.Copy(new byte[] { 1, 2, 3, 4 }, 0, samplePointer, 4);
            FakePacketSource source = new([
                new NativeCapturePacket(
                    Data: samplePointer,
                    FrameCount: 2,
                    Flags: AudioClientBufferFlags.None,
                    DevicePosition: 17,
                    QpcPosition: 23),
            ]);
            BoundedPcmPacketQueue sink = new(2, 16);

            int written = PcmPacketPump.DrainAvailable(
                source,
                format,
                sink,
                options);
            Require(
                written == 1
                && source.GetBufferCount == 1
                && source.ReleaseBufferCount == 1,
                "get-release-paired-on-copy",
                checks);
            Require(
                sink.TryRead(out PcmPacket copied)
                && copied.Data.Span.SequenceEqual(new byte[] { 1, 2, 3, 4 })
                && copied.FrameCount == 2
                && copied.DevicePosition == 17
                && copied.QpcPosition == 23,
                "pcm-packet-copied-before-release",
                checks);
        }
        finally
        {
            Marshal.FreeHGlobal(samplePointer);
        }

        FakePacketSource silentSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 2,
                Flags: AudioClientBufferFlags.Silent
                    | AudioClientBufferFlags.DataDiscontinuity,
                DevicePosition: 1,
                QpcPosition: 2),
        ]);
        BoundedPcmPacketQueue silentSink = new(1, 8);
        _ = PcmPacketPump.DrainAvailable(
            silentSource,
            format,
            silentSink,
            options);
        Require(
            silentSink.TryRead(out PcmPacket silent)
            && silent.Silent
            && silent.Discontinuous
            && silent.Data.Span.SequenceEqual(new byte[4])
            && silentSource.ReleaseBufferCount == 1,
            "silent-packet-zero-filled-and-released",
            checks);

        BoundedPcmPacketQueue fullSink = new(1, 4);
        Require(
            fullSink.TryWrite(new PcmPacket(
                new byte[4],
                FrameCount: 2,
                Silent: false,
                Discontinuous: false,
                TimestampError: false,
                DevicePosition: 0,
                QpcPosition: 0)),
            "bounded-sink-first-write",
            checks);
        FakePacketSource backpressureSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: AudioClientBufferFlags.Silent,
                DevicePosition: 0,
                QpcPosition: 0),
        ]);
        bool backpressureRejected = false;
        try
        {
            _ = PcmPacketPump.DrainAvailable(
                backpressureSource,
                format,
                fullSink,
                options);
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_SINK_BACKPRESSURE")
        {
            backpressureRejected = true;
        }

        Require(
            backpressureRejected
            && backpressureSource.ReleaseBufferCount == 1,
            "backpressure-fails-closed-after-release",
            checks);

        FakePacketSource oversizedSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 17,
                Flags: AudioClientBufferFlags.Silent,
                DevicePosition: 0,
                QpcPosition: 0),
        ]);
        bool oversizedRejected = false;
        try
        {
            _ = PcmPacketPump.DrainAvailable(
                oversizedSource,
                format,
                new BoundedPcmPacketQueue(2, 64),
                options);
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_PACKET_TOO_LARGE")
        {
            oversizedRejected = true;
        }

        Require(
            oversizedRejected
            && oversizedSource.ReleaseBufferCount == 1,
            "oversized-packet-released-and-rejected",
            checks);

        FakePacketSource emptySource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: AudioClientBufferFlags.Silent,
                DevicePosition: 0,
                QpcPosition: 0),
        ])
        {
            ReturnBufferEmptyOnce = true,
        };
        int emptyWritten = PcmPacketPump.DrainAvailable(
            emptySource,
            format,
            new BoundedPcmPacketQueue(1, 8),
            options);
        Require(
            emptyWritten == 0
            && emptySource.GetBufferCount == 1
            && emptySource.ReleaseBufferCount == 0,
            "buffer-empty-does-not-release",
            checks);

        FakePacketSource timestampSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: AudioClientBufferFlags.Silent
                    | AudioClientBufferFlags.TimestampError,
                DevicePosition: 99,
                QpcPosition: 101),
        ]);
        BoundedPcmPacketQueue timestampSink = new(1, 8);
        _ = PcmPacketPump.DrainAvailable(
            timestampSource,
            format,
            timestampSink,
            options);
        Require(
            timestampSink.TryRead(out PcmPacket timestampPacket)
            && timestampPacket.TimestampError
            && timestampPacket.DevicePosition == 0
            && timestampPacket.QpcPosition == 0,
            "timestamp-error-invalidates-positions",
            checks);

        FakePacketSource unknownFlagSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: (AudioClientBufferFlags)0x8,
                DevicePosition: 0,
                QpcPosition: 0),
        ]);
        bool unknownFlagRejected = false;
        try
        {
            _ = PcmPacketPump.DrainAvailable(
                unknownFlagSource,
                format,
                new BoundedPcmPacketQueue(1, 8),
                options);
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_PACKET_FLAGS_UNSUPPORTED")
        {
            unknownFlagRejected = true;
        }

        Require(
            unknownFlagRejected
            && unknownFlagSource.ReleaseBufferCount == 1,
            "unknown-packet-flags-released-and-rejected",
            checks);

        FakePacketSource missingDataSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: AudioClientBufferFlags.None,
                DevicePosition: 0,
                QpcPosition: 0),
        ]);
        bool missingDataRejected = false;
        try
        {
            _ = PcmPacketPump.DrainAvailable(
                missingDataSource,
                format,
                new BoundedPcmPacketQueue(1, 8),
                options);
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_PACKET_DATA_MISSING")
        {
            missingDataRejected = true;
        }

        Require(
            missingDataRejected
            && missingDataSource.ReleaseBufferCount == 1,
            "missing-packet-data-released-and-rejected",
            checks);

        FakePacketSource frameLimitSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 2,
                Flags: AudioClientBufferFlags.Silent,
                DevicePosition: 0,
                QpcPosition: 0),
        ])
        {
            MaximumFrameCount = 1,
        };
        bool frameLimitRejected = false;
        try
        {
            _ = PcmPacketPump.DrainAvailable(
                frameLimitSource,
                format,
                new BoundedPcmPacketQueue(1, 8),
                options);
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_FRAME_COUNT_EXCEEDED")
        {
            frameLimitRejected = true;
        }

        Require(
            frameLimitRejected
            && frameLimitSource.ReleaseBufferCount == 1,
            "frame-count-limit-released-and-rejected",
            checks);

        BoundedPcmPacketQueue aggregateSink = new(1, 2);
        Require(
            aggregateSink.TryWrite(new PcmPacket(
                new byte[2], 1, false, false, false, 0, 0)),
            "aggregate-error-sink-prefilled",
            checks);
        FakePacketSource releaseFailureSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: AudioClientBufferFlags.Silent,
                DevicePosition: 0,
                QpcPosition: 0),
        ])
        {
            ThrowOnRelease = true,
        };
        bool aggregateErrorReported = false;
        try
        {
            _ = PcmPacketPump.DrainAvailable(
                releaseFailureSource,
                format,
                aggregateSink,
                options);
        }
        catch (AggregateException exception)
            when (exception.InnerExceptions.Count == 2)
        {
            aggregateErrorReported = true;
        }

        Require(
            aggregateErrorReported
            && releaseFailureSource.ReleaseBufferCount == 1,
            "copy-and-release-errors-aggregated",
            checks);

        BoundedPcmPacketQueue capacitySink = new(2, 8);
        Require(
            capacitySink.TryWrite(new PcmPacket(
                new byte[4], 2, false, false, false, 0, 0))
            && capacitySink.TryWrite(new PcmPacket(
                new byte[4], 2, false, false, false, 0, 0))
            && !capacitySink.TryWrite(new PcmPacket(
                new byte[1], 1, false, false, false, 0, 0)),
            "bounded-sink-enforces-capacity",
            checks);
        Require(
            capacitySink.TryRead(out _)
            && capacitySink.QueuedBytes == 4
            && capacitySink.TryWrite(new PcmPacket(
                new byte[1], 1, false, false, false, 0, 0)),
            "bounded-sink-releases-capacity-on-read",
            checks);
    }

    private static void CheckCaptureThreadAffinity(
        ICollection<string> checks)
    {
        CaptureThreadAffinity affinity = new();
        affinity.BindOrAssert(101);
        affinity.BindOrAssert(101);
        bool crossThreadRejected = false;
        try
        {
            affinity.BindOrAssert(202);
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_CAPTURE_THREAD_MISMATCH")
        {
            crossThreadRejected = true;
        }

        Require(
            crossThreadRejected,
            "capture-client-thread-affinity",
            checks);
    }

    private static void CheckSessionLifecycle(ICollection<string> checks)
    {
        const uint sampleProcessId = 31180;

        BoundedPcmPacketQueue preparedSink = new(2, 16);
        using (ProcessLoopbackCaptureSession prepared =
            ProcessLoopbackCaptureSession.Prepare(
                sampleProcessId,
                preparedSink))
        {
            prepared.StopAsync().GetAwaiter().GetResult();
            Require(
                prepared.State == CaptureSessionState.Stopped
                && preparedSink.IsCompleted,
                "prepared-stop-does-not-activate-audio",
                checks);
        }

        BoundedPcmPacketQueue lifecycleSink = new(2, 16);
        FakeCaptureRuntimeFactory lifecycleFactory = new();
        ProcessLoopbackCaptureSession lifecycle =
            ProcessLoopbackCaptureSession.PrepareForTest(
                sampleProcessId,
                lifecycleSink,
                lifecycleFactory);
        lifecycle.StartAsync().GetAwaiter().GetResult();
        Require(
            SpinWait.SpinUntil(
                () => lifecycleFactory.Runtime.DrainCount == 1,
                TimeSpan.FromSeconds(2)),
            "dedicated-session-drains-after-event",
            checks);
        lifecycle.StopAsync().GetAwaiter().GetResult();
        Require(
            lifecycle.State == CaptureSessionState.Stopped
            && lifecycleFactory.Runtime.StartCount == 1
            && lifecycleFactory.Runtime.StopCount == 1
            && lifecycleFactory.Runtime.DisposeCount == 1
            && lifecycleSink.IsCompleted
            && lifecycleSink.CompletionError is null,
            "explicit-stop-completes-and-rolls-back",
            checks);
        int[] runtimeThreads = lifecycleFactory.Runtime.ThreadIds.ToArray();
        Require(
            runtimeThreads.Length >= 4
            && runtimeThreads.Distinct().Count() == 1
            && runtimeThreads[0] != Environment.CurrentManagedThreadId,
            "capture-runtime-stays-on-one-dedicated-thread",
            checks);
        lifecycle.Dispose();
        Require(
            lifecycle.State == CaptureSessionState.Disposed
            && lifecycleFactory.Runtime.DisposeCount == 1,
            "dispose-idempotent-after-stop",
            checks);

        BoundedPcmPacketQueue startFailureSink = new(2, 16);
        FakeCaptureRuntimeFactory startFailureFactory =
            new(failOnStart: true);
        ProcessLoopbackCaptureSession startFailure =
            ProcessLoopbackCaptureSession.PrepareForTest(
                sampleProcessId,
                startFailureSink,
                startFailureFactory);
        bool startFailureReported = false;
        try
        {
            startFailure.StartAsync().GetAwaiter().GetResult();
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_FAKE_START_FAILURE")
        {
            startFailureReported = true;
        }

        Require(
            startFailureReported
            && startFailure.State == CaptureSessionState.Faulted
            && startFailureFactory.Runtime.StopCount == 1
            && startFailureFactory.Runtime.DisposeCount == 1
            && startFailureSink.CompletionError is not null,
            "start-failure-rolls-back-runtime",
            checks);
        try
        {
            startFailure.Dispose();
        }
        catch (InvalidOperationException)
        {
            // Dispose preserves the already-observed terminal failure.
        }
        startFailure.Dispose();
        Require(
            startFailure.State == CaptureSessionState.Disposed
            && startFailureFactory.Runtime.DisposeCount == 1,
            "failed-process-dispose-still-finalizes-owned-runtime",
            checks);

        ThrowingCompleteSink throwingCompleteSink = new();
        ProcessLoopbackCaptureSession completionFailure =
            ProcessLoopbackCaptureSession.Prepare(
                sampleProcessId,
                throwingCompleteSink);
        bool completionFailureReported = false;
        try
        {
            completionFailure.StopAsync().GetAwaiter().GetResult();
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_FAKE_COMPLETE_FAILURE")
        {
            completionFailureReported = true;
        }

        Require(
            completionFailureReported
            && completionFailure.State == CaptureSessionState.Faulted
            && completionFailure.Completion.IsFaulted,
            "sink-complete-failure-still-settles-session",
            checks);
        try
        {
            completionFailure.Dispose();
        }
        catch (InvalidOperationException)
        {
            // Dispose keeps the terminal sink failure observable.
        }
        completionFailure.Dispose();
        Require(
            completionFailure.State == CaptureSessionState.Disposed,
            "failed-process-completion-dispose-still-finalizes-session",
            checks);

        int callbackFirstCleanupCount = 0;
        ActivationCleanupGate callbackFirstGate =
            new(() => callbackFirstCleanupCount++);
        callbackFirstGate.MarkCallbackFinished();
        Require(
            callbackFirstCleanupCount == 0,
            "activation-buffer-held-before-call-return",
            checks);
        callbackFirstGate.MarkCallReturned();
        callbackFirstGate.MarkCallReturned();
        Require(
            callbackFirstCleanupCount == 1,
            "activation-cleanup-after-result-and-call-return",
            checks);

        int failureCleanupCount = 0;
        ActivationCleanupGate failureGate = new(() => failureCleanupCount++);
        failureGate.MarkSynchronousFailure();
        Require(
            failureCleanupCount == 1,
            "activation-sync-failure-cleans-immediately",
            checks);

        TaskCompletionSource<int> abandonedResult =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        int abandonedReleaseCount = 0;
        Task abandonedObserver = AbandonedResultFinalizer.ObserveAsync(
            abandonedResult.Task,
            _ => abandonedReleaseCount++);
        Require(
            !abandonedObserver.IsCompleted && abandonedReleaseCount == 0,
            "abandoned-result-held-until-native-completion",
            checks);
        abandonedResult.SetResult(1);
        abandonedObserver.GetAwaiter().GetResult();
        Require(
            abandonedReleaseCount == 1,
            "abandoned-result-released-after-native-completion",
            checks);
    }

    private static void CheckExplicitMicrophoneLifecycle(
        ICollection<string> checks)
    {
        const string endpointId =
            "{0.0.1.00000000}.{00112233-4455-6677-8899-AABBCCDDEEFF}";
        MicCaptureRequest request = MicCaptureRequest.Create(endpointId);

        BoundedPcmPacketQueue preparedSink = new(2, 16);
        using (ExplicitMicrophoneCaptureSession prepared =
            ExplicitMicrophoneCaptureSession.Prepare(
                request,
                preparedSink))
        {
            prepared.StopAsync().GetAwaiter().GetResult();
            Require(
                prepared.State == CaptureSessionState.Stopped
                && preparedSink.IsCompleted,
                "prepared-mic-stop-does-not-open-device",
                checks);
        }

        BoundedPcmPacketQueue lifecycleSink = new(2, 16);
        FakeMicCaptureRuntimeFactory lifecycleFactory = new();
        ExplicitMicrophoneCaptureSession lifecycle =
            ExplicitMicrophoneCaptureSession.PrepareForTest(
                request,
                lifecycleSink,
                lifecycleFactory);
        lifecycle.StartAsync().GetAwaiter().GetResult();
        Require(
            SpinWait.SpinUntil(
                () => lifecycleFactory.Runtime.DrainCount == 1,
                TimeSpan.FromSeconds(2)),
            "mic-dedicated-session-drains-after-event",
            checks);
        lifecycle.StopAsync().GetAwaiter().GetResult();
        Require(
            lifecycleFactory.ObservedEndpointId == endpointId
            && lifecycleFactory.Runtime.StartCount == 1
            && lifecycleFactory.Runtime.StopCount == 1
            && lifecycleFactory.Runtime.DisposeCount == 1
            && lifecycle.State == CaptureSessionState.Stopped
            && lifecycleSink.IsCompleted
            && lifecycleSink.CompletionError is null,
            "mic-explicit-id-propagates-and-stops-cleanly",
            checks);
        int[] runtimeThreads = lifecycleFactory.Runtime.ThreadIds.ToArray();
        Require(
            runtimeThreads.Length >= 4
            && runtimeThreads.Distinct().Count() == 1
            && runtimeThreads[0] != Environment.CurrentManagedThreadId,
            "mic-runtime-stays-on-one-dedicated-thread",
            checks);
        lifecycle.Dispose();

        BoundedPcmPacketQueue initializeFailureSink = new(2, 16);
        FakeMicCaptureRuntimeFactory initializeFailureFactory =
            new(failOnInitialize: true);
        ExplicitMicrophoneCaptureSession initializeFailure =
            ExplicitMicrophoneCaptureSession.PrepareForTest(
                request,
                initializeFailureSink,
                initializeFailureFactory);
        bool initializeFailureReported = false;
        try
        {
            initializeFailure.StartAsync().GetAwaiter().GetResult();
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_FAKE_MIC_INITIALIZE_FAILURE")
        {
            initializeFailureReported = true;
        }

        Require(
            initializeFailureReported
            && initializeFailure.State == CaptureSessionState.Faulted
            && initializeFailureFactory.Runtime.StartCount == 0
            && initializeFailureFactory.Runtime.StopCount == 0
            && initializeFailureFactory.Runtime.DisposeCount == 1
            && initializeFailureSink.CompletionError is not null,
            "mic-initialize-failure-disposes-without-start",
            checks);
        try
        {
            initializeFailure.Dispose();
        }
        catch (InvalidOperationException)
        {
            // Dispose keeps the already-observed initialization failure.
        }
        initializeFailure.Dispose();
        Require(
            initializeFailure.State == CaptureSessionState.Disposed
            && initializeFailureFactory.Runtime.DisposeCount == 1,
            "failed-mic-dispose-still-finalizes-owned-runtime",
            checks);

        CaptureSessionOptions pumpOptions = new()
        {
            MaximumPacketBytes = 8,
            MaximumPacketsPerWake = 2,
        };
        FakePacketSource silentSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: AudioClientBufferFlags.Silent,
                DevicePosition: 1,
                QpcPosition: 2),
        ]);
        BoundedPcmPacketQueue silentSink = new(1, 4);
        _ = PcmPacketPump.DrainAvailable(
            silentSource,
            TestFormat(),
            silentSink,
            pumpOptions);
        Require(
            silentSink.TryRead(out PcmPacket silent)
            && silent.Silent
            && silent.Data.Span.SequenceEqual(new byte[2]),
            "mic-shared-pump-silent-packet",
            checks);

        FakePacketSource emptySource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: AudioClientBufferFlags.Silent,
                DevicePosition: 0,
                QpcPosition: 0),
        ])
        {
            ReturnBufferEmptyOnce = true,
        };
        Require(
            PcmPacketPump.DrainAvailable(
                emptySource,
                TestFormat(),
                new BoundedPcmPacketQueue(1, 4),
                pumpOptions) == 0
            && emptySource.ReleaseBufferCount == 0,
            "mic-shared-pump-empty-buffer",
            checks);

        BoundedPcmPacketQueue fullSink = new(1, 2);
        _ = fullSink.TryWrite(
            new PcmPacket(
                new byte[2],
                1,
                false,
                false,
                false,
                0,
                0));
        FakePacketSource blockedSource = new([
            new NativeCapturePacket(
                Data: 0,
                FrameCount: 1,
                Flags: AudioClientBufferFlags.Silent,
                DevicePosition: 0,
                QpcPosition: 0),
        ]);
        bool backpressureRejected = false;
        try
        {
            _ = PcmPacketPump.DrainAvailable(
                blockedSource,
                TestFormat(),
                fullSink,
                pumpOptions);
        }
        catch (InvalidOperationException exception)
            when (exception.Message
                == "BW_COMPUTER_VOICE_AUDIO_SINK_BACKPRESSURE")
        {
            backpressureRejected = true;
        }

        Require(
            backpressureRejected
            && blockedSource.ReleaseBufferCount == 1,
            "mic-shared-pump-backpressure-fails-closed",
            checks);
    }

    private static void CheckInteropVtables(ICollection<string> checks)
    {
        string[] audioClientMethods =
            typeof(IAudioClient).GetMethods().Select(method => method.Name).ToArray();
        Require(
            audioClientMethods.SequenceEqual(new[]
            {
                "Initialize",
                "GetBufferSize",
                "GetStreamLatency",
                "GetCurrentPadding",
                "IsFormatSupported",
                "GetMixFormat",
                "GetDevicePeriod",
                "Start",
                "Stop",
                "Reset",
                "SetEventHandle",
                "GetService",
            }),
            "iaudioclient-vtable-12-methods-in-order",
            checks);

        string[] captureClientMethods =
            typeof(IAudioCaptureClient)
                .GetMethods()
                .Select(method => method.Name)
                .ToArray();
        Require(
            captureClientMethods.SequenceEqual(new[]
            {
                "GetBuffer",
                "ReleaseBuffer",
                "GetNextPacketSize",
            }),
            "iaudiocaptureclient-vtable-3-methods-in-order",
            checks);

        string[] endpointVolumeMethods =
            typeof(IAudioEndpointVolumeForBridge)
                .GetMethods()
                .Select(method => method.Name)
                .ToArray();
        Require(
            endpointVolumeMethods.SequenceEqual(new[]
            {
                "RegisterControlChangeNotify",
                "UnregisterControlChangeNotify",
                "GetChannelCount",
                "SetMasterVolumeLevel",
                "SetMasterVolumeLevelScalar",
                "GetMasterVolumeLevel",
                "GetMasterVolumeLevelScalar",
                "SetChannelVolumeLevel",
                "SetChannelVolumeLevelScalar",
                "GetChannelVolumeLevel",
                "GetChannelVolumeLevelScalar",
                "SetMute",
                "GetMute",
            }),
            "iaudioendpointvolume-vtable-through-mute-is-exact",
            checks);

        string[] deviceEnumeratorMethods =
            typeof(IMMDeviceEnumerator)
                .GetMethods()
                .Select(method => method.Name)
                .ToArray();
        Require(
            deviceEnumeratorMethods.SequenceEqual(new[]
            {
                "ForbiddenEnumAudioEndpoints",
                "ForbiddenGetDefaultAudioEndpoint",
                "GetDevice",
                "RegisterEndpointNotificationCallback",
                "UnregisterEndpointNotificationCallback",
            }),
            "immdeviceenumerator-vtable-explicit-getdevice-slot",
            checks);

        string[] selectionEnumeratorMethods =
            typeof(IMMDeviceEnumeratorForSelection)
                .GetMethods()
                .Select(method => method.Name)
                .ToArray();
        Require(
            selectionEnumeratorMethods.SequenceEqual(new[]
            {
                "EnumAudioEndpoints",
                "GetDefaultAudioEndpoint",
                "GetDevice",
                "RegisterEndpointNotificationCallback",
                "UnregisterEndpointNotificationCallback",
            })
            && typeof(IMMDeviceEnumeratorForSelection)
                .GetMethod("GetDefaultAudioEndpoint")!
                .GetCustomAttribute<ObsoleteAttribute>()?.IsError
                == true,
            "mic-discovery-vtable-enumerates-with-default-slot-forbidden",
            checks);

        Require(
            typeof(IMMDeviceCollectionForSelection)
                .GetMethods()
                .Select(method => method.Name)
                .SequenceEqual(new[] { "GetCount", "Item" }),
            "mic-discovery-collection-vtable-is-exact",
            checks);

        string[] deviceMethods =
            typeof(IMMDevice)
                .GetMethods()
                .Select(method => method.Name)
                .ToArray();
        Require(
            deviceMethods.SequenceEqual(new[]
            {
                "Activate",
                "OpenPropertyStore",
                "GetId",
                "GetState",
            }),
            "immdevice-vtable-4-methods-in-order",
            checks);

        MethodInfo? nativeActivation = typeof(ProcessLoopbackInterop).GetMethod(
            nameof(ProcessLoopbackInterop.ActivateAudioInterfaceAsync),
            BindingFlags.Static | BindingFlags.NonPublic);
        DllImportAttribute? import =
            nativeActivation?.GetCustomAttribute<DllImportAttribute>();
        Require(
            import?.Value == "Mmdevapi.dll"
            && import.EntryPoint == "ActivateAudioInterfaceAsync",
            "mmdevapi-entrypoint",
            checks);
    }

    private static void CheckVirtualMicrophoneRenderContract(
        ICollection<string> checks)
    {
        WaveFormatEx format =
            NativeVirtualMicrophoneRenderRuntime.RenderFormatForTest;
        AudioClientStreamFlags flags =
            NativeVirtualMicrophoneRenderRuntime.StreamFlagsForTest;
        Require(
            format.FormatTag == PcmAudioFormat.WaveFormatPcm
            && format.Channels == 1
            && format.SamplesPerSecond == 48_000
            && format.AverageBytesPerSecond == 96_000
            && format.BlockAlign == 2
            && format.BitsPerSample == 16
            && format.ExtraSize == 0
            && flags
                == (
                    AudioClientStreamFlags.EventCallback
                    | AudioClientStreamFlags.AutoConvertPcm
                    | AudioClientStreamFlags.SrcDefaultQuality),
            "virtual-mic-render-fixed-pcm48-native-flags",
            checks);

        Require(
            typeof(IAudioRenderClient)
                .GetMethods()
                .Select(method => method.Name)
                .SequenceEqual(new[] { "GetBuffer", "ReleaseBuffer" })
            && ProcessLoopbackInterop.IidIAudioRenderClient
                == new Guid("F294ACFC-3146-4483-A7BF-ADDCA7C260E2"),
            "iaudiorenderclient-vtable-and-iid-are-exact",
            checks);

        BoundedUplinkPcmQueue queue = new();
        byte[] ownedSource = Enumerable.Repeat(
            (byte)0x31,
            Pcm48kMonoFramer.BytesPerChunk).ToArray();
        queue.Push(ownedSource);
        Array.Fill(ownedSource, (byte)0x72);
        byte[] read = new byte[Pcm48kMonoFramer.BytesPerChunk];
        int copied = queue.Read(read);
        int underflow = queue.Read(read);
        Require(
            copied == Pcm48kMonoFramer.BytesPerChunk
            && read.All(value => value == 0x31)
            && underflow == 0
            && queue.BufferedFrames == 0,
            "virtual-mic-uplink-queue-owns-copy-and-underflows-empty",
            checks);

        for (int index = 0;
            index < BoundedUplinkPcmQueue.MaximumFrames;
            index++)
        {
            queue.Push(Enumerable.Repeat(
                checked((byte)index),
                Pcm48kMonoFramer.BytesPerChunk).ToArray());
        }
        queue.Push(Enumerable.Repeat(
            (byte)BoundedUplinkPcmQueue.MaximumFrames,
            Pcm48kMonoFramer.BytesPerChunk).ToArray());
        byte[] oldestRetained =
            new byte[Pcm48kMonoFramer.BytesPerChunk];
        int retainedBytes = queue.Read(oldestRetained);
        queue.StopAndClear();
        bool stoppedRejected = false;
        try
        {
            queue.Push(new byte[Pcm48kMonoFramer.BytesPerChunk]);
        }
        catch (DirectProtocolException exception)
        {
            stoppedRejected = exception.Code
                == "BW_COMPUTER_VOICE_DIRECT_UPLINK_NOT_ACTIVE";
        }
        Require(
            BoundedUplinkPcmQueue.MaximumBufferedMilliseconds == 200
            && BoundedUplinkPcmQueue.MaximumFrames == 10
            && queue.DroppedFrames == 1
            && retainedBytes == Pcm48kMonoFramer.BytesPerChunk
            && oldestRetained.All(value => value == 1)
            && stoppedRejected
            && queue.BufferedFrames == 0,
            "virtual-mic-uplink-queue-drops-oldest-and-stays-bounded",
            checks);

        FakeVirtualRenderRuntimeFactory factory = new();
        VirtualMicrophoneRenderSession session =
            VirtualMicrophoneRenderSession.PrepareForTest(
                VirtualMicrophoneRenderRequest.Create(
                    "test-virtual-microphone-render"),
                factory);
        try
        {
            session.StartAsync().GetAwaiter().GetResult();
            byte[] payload = Enumerable.Range(
                0,
                Pcm48kMonoFramer.BytesPerChunk)
                .Select(value => (byte)value)
                .ToArray();
            session.Push(new DirectPcmFrame(
                DirectPcmTrack.BrowserMicrophone,
                Sequence: 0,
                TimestampMicroseconds: 20_000,
                PcmS16Le: payload));
            Array.Clear(payload);
            factory.Runtime.SignalAudioReady();
            factory.Runtime.RenderObserved.Wait(
                TimeSpan.FromSeconds(2));
            session.StopAsync().GetAwaiter().GetResult();
            Require(
                factory.EndpointId
                    == "test-virtual-microphone-render"
                && factory.Runtime.InitializeThreadId != 0
                && factory.Runtime.InitializeOrder == 1
                && factory.Runtime.PrimeOrder == 2
                && factory.Runtime.StartOrder == 3
                && factory.Runtime.InitializeThreadId
                    == factory.Runtime.PrimeThreadId
                && factory.Runtime.InitializeThreadId
                    == factory.Runtime.StartThreadId
                && factory.Runtime.InitializeThreadId
                    == factory.Runtime.RenderThreadId
                && factory.Runtime.InitializeThreadId
                    == factory.Runtime.StopThreadId
                && factory.Runtime.InitializeThreadId
                    == factory.Runtime.DisposeThreadId
                && factory.Runtime.RenderedPcm.Length
                    == Pcm48kMonoFramer.BytesPerChunk
                && factory.Runtime.RenderedPcm[1] == 1
                && factory.Runtime.RenderedPcm[255] == 255
                && session.State == CaptureSessionState.Stopped
                && session.BufferedFrames == 0,
                "virtual-mic-render-session-is-dedicated-owned-and-no-real-audio",
                checks);
        }
        finally
        {
            session.Dispose();
        }

        FakeVirtualRenderRuntimeFactory fallbackFactory = new();
        VirtualMicrophoneRenderSession fallbackSession =
            VirtualMicrophoneRenderSession.PrepareForTest(
                VirtualMicrophoneRenderRequest.Create(
                    "test-virtual-microphone-render-fallback"),
                fallbackFactory);
        try
        {
            fallbackSession.StartAsync().GetAwaiter().GetResult();
            fallbackSession.Push(new DirectPcmFrame(
                DirectPcmTrack.BrowserMicrophone,
                Sequence: 0,
                TimestampMicroseconds: 20_000,
                PcmS16Le: Enumerable.Repeat(
                    (byte)0x4a,
                    Pcm48kMonoFramer.BytesPerChunk).ToArray()));
            bool renderedWithoutSignal =
                fallbackFactory.Runtime.RenderObserved.Wait(
                    TimeSpan.FromSeconds(2));
            fallbackSession.StopAsync().GetAwaiter().GetResult();
            Require(
                VirtualMicrophoneRenderSession
                    .RenderWakeFallbackMilliseconds == 100
                && renderedWithoutSignal
                && fallbackFactory.Runtime.RenderedPcm.Length
                    == Pcm48kMonoFramer.BytesPerChunk
                && fallbackFactory.Runtime.RenderedPcm.All(
                    value => value == 0x4a),
                "virtual-mic-render-session-recovers-missed-first-event",
                checks);
        }
        finally
        {
            fallbackSession.Dispose();
        }
    }

    private static PcmAudioFormat TestFormat() =>
        new(
            PcmSampleEncoding.IntegerPcm,
            FormatTag: 1,
            Channels: 1,
            SamplesPerSecond: 8000,
            AverageBytesPerSecond: 16000,
            BlockAlign: 2,
            BitsPerSample: 16,
            ValidBitsPerSample: 16,
            ExtraSize: 0,
            ChannelMask: 0,
            SubFormat: PcmAudioFormat.SubtypePcm);

    private static PcmAudioFormat ParseNativeFormat<T>(T native)
        where T : struct
    {
        nint pointer = Marshal.AllocHGlobal(Marshal.SizeOf<T>());
        try
        {
            Marshal.StructureToPtr(native, pointer, false);
            return PcmAudioFormat.FromNative(pointer);
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
    }

    private static void Require(
        bool condition,
        string name,
        ICollection<string> checks)
    {
        if (!condition)
        {
            throw new InvalidOperationException($"self-test failed: {name}");
        }

        checks.Add(name);
    }

    private sealed class FakeVirtualRenderRuntimeFactory :
        IVirtualMicrophoneRenderRuntimeFactory
    {
        internal FakeVirtualRenderRuntime Runtime { get; } = new();

        internal string? EndpointId { get; private set; }

        public IVirtualMicrophoneRenderRuntime Create(
            VirtualMicrophoneRenderRequest request,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            EndpointId = request.EndpointId;
            return Runtime;
        }
    }

    private sealed class FakeVirtualRenderRuntime :
        IVirtualMicrophoneRenderRuntime
    {
        private EventWaitHandle? _audioReady;
        private int _callOrder;

        internal ManualResetEventSlim RenderObserved { get; } =
            new(initialState: false);

        internal int InitializeThreadId { get; private set; }

        internal int InitializeOrder { get; private set; }

        internal int PrimeThreadId { get; private set; }

        internal int PrimeOrder { get; private set; }

        internal int StartThreadId { get; private set; }

        internal int StartOrder { get; private set; }

        internal int RenderThreadId { get; private set; }

        internal int StopThreadId { get; private set; }

        internal int DisposeThreadId { get; private set; }

        internal byte[] RenderedPcm { get; private set; } = [];

        public void Initialize(EventWaitHandle audioReadyEvent)
        {
            _audioReady = audioReadyEvent;
            InitializeThreadId = Environment.CurrentManagedThreadId;
            InitializeOrder = ++_callOrder;
        }

        public void Prime()
        {
            PrimeThreadId = Environment.CurrentManagedThreadId;
            PrimeOrder = ++_callOrder;
        }

        public void Start()
        {
            StartThreadId = Environment.CurrentManagedThreadId;
            StartOrder = ++_callOrder;
        }

        public void Render(BoundedUplinkPcmQueue source)
        {
            RenderThreadId = Environment.CurrentManagedThreadId;
            byte[] destination =
                new byte[Pcm48kMonoFramer.BytesPerChunk];
            int read = source.Read(destination);
            RenderedPcm = destination.AsSpan(0, read).ToArray();
            RenderObserved.Set();
        }

        internal void SignalAudioReady()
        {
            (_audioReady
                ?? throw new InvalidOperationException(
                    "fake render runtime was not initialized")).Set();
        }

        public void Stop()
        {
            StopThreadId = Environment.CurrentManagedThreadId;
        }

        public void Dispose()
        {
            DisposeThreadId = Environment.CurrentManagedThreadId;
        }
    }

    private sealed class FakePacketSource : ICapturePacketSource
    {
        private readonly Queue<NativeCapturePacket> _packets;
        private bool _bufferLeased;

        internal FakePacketSource(IEnumerable<NativeCapturePacket> packets)
        {
            _packets = new Queue<NativeCapturePacket>(packets);
        }

        internal int GetBufferCount { get; private set; }

        internal int ReleaseBufferCount { get; private set; }

        internal bool ReturnBufferEmptyOnce { get; init; }

        internal bool ThrowOnRelease { get; init; }

        public uint MaximumFrameCount { get; init; } = 1024;

        public uint GetNextPacketSize() =>
            _packets.TryPeek(out NativeCapturePacket packet)
                ? packet.FrameCount
                : 0;

        public bool TryGetBuffer(out NativeCapturePacket packet)
        {
            GetBufferCount++;
            if (ReturnBufferEmptyOnce && GetBufferCount == 1)
            {
                packet = default;
                return false;
            }

            if (_bufferLeased || !_packets.TryPeek(out packet))
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_FAKE_BUFFER_STATE");
            }

            _bufferLeased = true;
            return true;
        }

        public void ReleaseBuffer(uint frameCount)
        {
            ReleaseBufferCount++;
            if (ThrowOnRelease)
            {
                _bufferLeased = false;
                _packets.TryDequeue(out _);
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_FAKE_RELEASE_FAILURE");
            }

            if (!_bufferLeased
                || !_packets.TryDequeue(out NativeCapturePacket packet)
                || packet.FrameCount != frameCount)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_FAKE_RELEASE_STATE");
            }

            _bufferLeased = false;
        }

        public void Dispose()
        {
        }
    }

    private sealed class FakeNativeAudioClientLease :
        INativeAudioClientLease
    {
        public IAudioClient AudioClient =>
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_FAKE_CLIENT_NOT_AVAILABLE");

        internal int DisposeCount { get; private set; }

        public void Dispose()
        {
            DisposeCount++;
        }
    }

    private sealed class FakeCaptureRuntimeFactory :
        IProcessLoopbackCaptureRuntimeFactory
    {
        internal FakeCaptureRuntimeFactory(bool failOnStart = false)
        {
            Runtime = new FakeCaptureRuntime(failOnStart);
        }

        internal FakeCaptureRuntime Runtime { get; }

        public IProcessLoopbackCaptureRuntime Create(
            uint targetProcessId,
            TimeSpan activationTimeout,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Runtime.ThreadIds.Add(Environment.CurrentManagedThreadId);
            return Runtime;
        }
    }

    private sealed class FakeCaptureRuntime :
        IProcessLoopbackCaptureRuntime
    {
        private readonly bool _failOnStart;
        private EventWaitHandle? _event;

        internal FakeCaptureRuntime(bool failOnStart)
        {
            _failOnStart = failOnStart;
        }

        internal List<int> ThreadIds { get; } = [];

        internal int StartCount { get; private set; }

        internal int DrainCount { get; private set; }

        internal int StopCount { get; private set; }

        internal int DisposeCount { get; private set; }

        public PcmAudioFormat Initialize(EventWaitHandle audioReadyEvent)
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            _event = audioReadyEvent;
            return TestFormat();
        }

        public void Start()
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            StartCount++;
            if (_failOnStart)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_FAKE_START_FAILURE");
            }

            _event!.Set();
        }

        public int Drain(
            IBoundedPcmSink sink,
            CaptureSessionOptions options)
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            DrainCount++;
            return 0;
        }

        public void Stop()
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            StopCount++;
        }

        public void Dispose()
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            DisposeCount++;
        }
    }

    private sealed class FakeMicCaptureRuntimeFactory :
        IExplicitMicrophoneCaptureRuntimeFactory
    {
        internal FakeMicCaptureRuntimeFactory(
            bool failOnInitialize = false)
        {
            Runtime = new FakeMicCaptureRuntime(failOnInitialize);
        }

        internal FakeMicCaptureRuntime Runtime { get; }

        internal string? ObservedEndpointId { get; private set; }

        public IProcessLoopbackCaptureRuntime Create(
            MicCaptureRequest request,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            ObservedEndpointId = request.EndpointId;
            Runtime.ThreadIds.Add(Environment.CurrentManagedThreadId);
            return Runtime;
        }
    }

    private sealed class FakeMicCaptureRuntime :
        IProcessLoopbackCaptureRuntime
    {
        private readonly bool _failOnInitialize;
        private EventWaitHandle? _event;

        internal FakeMicCaptureRuntime(bool failOnInitialize)
        {
            _failOnInitialize = failOnInitialize;
        }

        internal List<int> ThreadIds { get; } = [];

        internal int StartCount { get; private set; }

        internal int DrainCount { get; private set; }

        internal int StopCount { get; private set; }

        internal int DisposeCount { get; private set; }

        public PcmAudioFormat Initialize(EventWaitHandle audioReadyEvent)
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            if (_failOnInitialize)
            {
                throw new InvalidOperationException(
                    "BW_COMPUTER_VOICE_AUDIO_FAKE_MIC_INITIALIZE_FAILURE");
            }

            _event = audioReadyEvent;
            return TestFormat();
        }

        public void Start()
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            StartCount++;
            _event!.Set();
        }

        public int Drain(
            IBoundedPcmSink sink,
            CaptureSessionOptions options)
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            DrainCount++;
            return 0;
        }

        public void Stop()
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            StopCount++;
        }

        public void Dispose()
        {
            ThreadIds.Add(Environment.CurrentManagedThreadId);
            DisposeCount++;
        }
    }

    private sealed class ThrowingCompleteSink : IBoundedPcmSink
    {
        public bool TryWrite(PcmPacket packet) => true;

        public void Complete(Exception? error) =>
            throw new InvalidOperationException(
                "BW_COMPUTER_VOICE_AUDIO_FAKE_COMPLETE_FAILURE");
    }

    private sealed class FakeCaptureEndpointMuteBackend :
        IDirectCaptureEndpointMuteBackend
    {
        private readonly bool _ignoreUnmute;

        internal FakeCaptureEndpointMuteBackend(
            bool initialMuted,
            bool ignoreUnmute = false)
        {
            Muted = initialMuted;
            _ignoreUnmute = ignoreUnmute;
        }

        internal bool Muted { get; set; }

        internal List<bool> Writes { get; } = [];

        internal List<string> EndpointIds { get; } = [];

        public bool ReadMuted(string endpointId)
        {
            EndpointIds.Add(endpointId);
            return Muted;
        }

        public void WriteMuted(string endpointId, bool muted)
        {
            EndpointIds.Add(endpointId);
            Writes.Add(muted);
            if (!(_ignoreUnmute && !muted))
            {
                Muted = muted;
            }
        }
    }
}
