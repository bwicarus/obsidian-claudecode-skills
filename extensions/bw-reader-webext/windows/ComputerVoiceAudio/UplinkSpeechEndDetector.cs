using System.Buffers.Binary;

namespace BwReader.ComputerVoiceAudio;

/// <summary>
/// 从设备麦克风的上行 PCM 里判「一句话说完了」。
///
/// 用户 2026-09-05 定的形状：「不断固定我停止说话时刻的快照，AI 查看时看到的就是
/// 最后一次被固定的快照」。说完的时刻 Codex 的语音模式自己知道、桥看不到；
/// 桥能看到的是同一路音频 —— 所以在这里自己判。判定放在上行帧这一层，
/// 音频从哪个通道进来都一样：只要把设备麦克风的帧推到协调器，说完就会触发。
///
/// 纯能量法，故意不上模型：每帧 RMS，自适应噪底（慢升快降，说话声抬不高它），
/// 连续 OnsetFrames 帧高于门限算开口，之后连续 ReleaseFrames 帧低于门限算说完；
/// 太短的一段（MinimumUtteranceFrames 以下）当噪声。只报「说完」这一个事件 ——
/// 读取方不需要知道过程，多报的事件只会多钉几次，不会错。
/// </summary>
internal sealed class UplinkSpeechEndDetector
{
    /// 一帧 20 ms（Pcm48kMonoFramer.FramesPerChunk / 48 kHz）。
    internal const int FrameMilliseconds = 20;

    /// 连续 6 帧（120 ms）有声才算开口 —— 咳嗽、碰麦克风一两帧就过去了。
    internal const int OnsetFrames = 6;

    /// 连续 35 帧（700 ms）无声才算说完 —— 句中换气通常在 300–500 ms。
    internal const int ReleaseFrames = 35;

    /// 短于 15 帧（300 ms）的一段不当一句话。
    internal const int MinimumUtteranceFrames = 15;

    /// s16 RMS 的绝对门限下限：再安静的环境也不把 250 以下当人声。
    internal const double AbsoluteFloor = 250;

    /// 有声门限 = 噪底 × 这个倍数（与 AbsoluteFloor 取大）。
    internal const double FloorRatio = 2.5;

    private double _noiseFloor = 400;
    private int _voicedRun;
    private int _silentRun;
    private int _utteranceFrames;
    private bool _speaking;

    internal bool Speaking => _speaking;

    internal double NoiseFloor => _noiseFloor;

    /// 最近一帧的 RMS 与有声判定。双工诊断用：AI 出声期间上行还有没有人声。
    internal double LastRms { get; private set; }

    internal bool LastVoiced { get; private set; }

    /// <summary>
    /// 喂一帧 s16le 单声道 PCM。返回 true 表示这一帧结束时判定一句话说完了。
    /// </summary>
    internal bool Observe(ReadOnlySpan<byte> pcmS16Le)
    {
        double rms = Rms(pcmS16Le);
        double threshold = Math.Max(AbsoluteFloor, _noiseFloor * FloorRatio);
        bool voiced = rms > threshold;
        LastRms = rms;
        LastVoiced = voiced;

        if (!voiced || !_speaking)
        {
            // 噪底只在无声（或还没开口）时跟踪，降得快、升得慢：
            // 说话声不能把噪底抬到把自己淹掉。
            double target = Math.Max(60, rms);
            double rate = target < _noiseFloor ? 0.2 : 0.02;
            _noiseFloor += (target - _noiseFloor) * rate;
        }

        if (voiced)
        {
            _voicedRun++;
            _silentRun = 0;
        }
        else
        {
            _silentRun++;
            _voicedRun = 0;
        }

        if (!_speaking)
        {
            if (_voicedRun >= OnsetFrames)
            {
                _speaking = true;
                _utteranceFrames = _voicedRun;
            }
            return false;
        }

        _utteranceFrames++;
        if (_silentRun < ReleaseFrames)
        {
            return false;
        }
        _speaking = false;
        int spokenFrames = _utteranceFrames - _silentRun;
        _utteranceFrames = 0;
        return spokenFrames >= MinimumUtteranceFrames;
    }

    internal static double Rms(ReadOnlySpan<byte> pcmS16Le)
    {
        int samples = pcmS16Le.Length / sizeof(short);
        if (samples == 0)
        {
            return 0;
        }
        double sum = 0;
        for (int index = 0; index < samples; index++)
        {
            double sample = BinaryPrimitives.ReadInt16LittleEndian(
                pcmS16Le.Slice(index * sizeof(short), sizeof(short)));
            sum += sample * sample;
        }
        return Math.Sqrt(sum / samples);
    }
}
