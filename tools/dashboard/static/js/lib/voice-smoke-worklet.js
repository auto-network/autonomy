class VoiceSmokePcmProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const processorOptions = (options && options.processorOptions) || {};
    this.targetSampleRate = Number(processorOptions.targetSampleRate) || 16000;
    this.frameSamples = Number(processorOptions.frameSamples) || 1600;
    this.inputSampleRate = sampleRate;
    this.sourceBuffer = [];
    this.sourceOffset = 0;
    this.ratio = this.inputSampleRate / this.targetSampleRate;
  }

  process(inputs) {
    const channel = inputs && inputs[0] && inputs[0][0];
    if (!channel || !channel.length) {
      return true;
    }

    for (let i = 0; i < channel.length; i += 1) {
      this.sourceBuffer.push(channel[i]);
    }

    this._emitFrames();
    return true;
  }

  _emitFrames() {
    while (this._hasEnoughForFrame()) {
      const pcm = new Int16Array(this.frameSamples);
      for (let i = 0; i < this.frameSamples; i += 1) {
        const sourceIndex = this.sourceOffset + (i * this.ratio);
        const lowIndex = Math.floor(sourceIndex);
        const highIndex = Math.min(lowIndex + 1, this.sourceBuffer.length - 1);
        const frac = sourceIndex - lowIndex;
        const low = this.sourceBuffer[lowIndex] || 0;
        const high = this.sourceBuffer[highIndex] || low;
        const sample = low + ((high - low) * frac);
        const clipped = Math.max(-1, Math.min(1, sample));
        pcm[i] = clipped < 0 ? clipped * 0x8000 : clipped * 0x7fff;
      }

      const nextStart = this.sourceOffset + (this.frameSamples * this.ratio);
      const dropCount = Math.floor(nextStart);
      if (dropCount > 0) {
        this.sourceBuffer.splice(0, dropCount);
      }
      this.sourceOffset = nextStart - dropCount;

      this.port.postMessage(
        { type: "audio", buffer: pcm.buffer },
        [pcm.buffer],
      );
    }
  }

  _hasEnoughForFrame() {
    const required = Math.floor(
      this.sourceOffset + ((this.frameSamples - 1) * this.ratio),
    ) + 2;
    return this.sourceBuffer.length >= required;
  }
}

registerProcessor("voice-smoke-pcm", VoiceSmokePcmProcessor);
