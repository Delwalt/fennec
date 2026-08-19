const elements = {
  connect: document.querySelector("#connect"),
  mute: document.querySelector("#mute"),
  tone: document.querySelector("#tone"),
  disconnect: document.querySelector("#disconnect"),
  microphone: document.querySelector("#microphone"),
  remoteAudio: document.querySelector("#remote-audio"),
  resumeAudio: document.querySelector("#resume-audio"),
  status: document.querySelector("#status"),
  statusDot: document.querySelector("#status-dot"),
  diagnostics: document.querySelector("#diagnostics"),
  events: document.querySelector("#events"),
  conversationState: document.querySelector("#conversation-state"),
  conversation: document.querySelector("#conversation"),
  voiceSettings: document.querySelector("#voice-settings"),
  resetSettings: document.querySelector("#reset-settings"),
  settingsStatus: document.querySelector("#settings-status"),
  sttModel: document.querySelector("#stt-model"),
  ttsModel: document.querySelector("#tts-model"),
  ttsVoice: document.querySelector("#tts-voice"),
  speechLanguage: document.querySelector("#speech-language"),
  vadThreshold: document.querySelector("#vad-threshold"),
  vadThresholdValue: document.querySelector("#vad-threshold-value"),
  endpointSilence: document.querySelector("#endpoint-silence"),
  prefixAudio: document.querySelector("#prefix-audio"),
  minSpeech: document.querySelector("#min-speech"),
  maxTurn: document.querySelector("#max-turn"),
  echoCancellation: document.querySelector("#echo-cancellation"),
  noiseSuppression: document.querySelector("#noise-suppression"),
  autoGain: document.querySelector("#auto-gain"),
};

let peer = null;
let control = null;
let microphoneStream = null;
let session = null;
let serverDefaults = null;
const assistantMessages = new Map();

function record(message) {
  const item = document.createElement("li");
  item.textContent = `${new Date().toLocaleTimeString()} · ${message}`;
  elements.events.prepend(item);
  while (elements.events.children.length > 8) elements.events.lastElementChild.remove();
}

function setStatus(label, state = "") {
  elements.status.textContent = label;
  elements.statusDot.className = `dot ${state}`;
}

function setConnected(connected) {
  document.body.classList.toggle("live", connected);
  elements.connect.disabled = connected;
  elements.mute.disabled = !connected;
  elements.tone.disabled = !connected || control?.readyState !== "open";
  elements.disconnect.disabled = !connected;
  elements.microphone.disabled = !connected;
  elements.voiceSettings.disabled = connected || serverDefaults === null;
  elements.resetSettings.disabled = connected || serverDefaults === null;
  if (serverDefaults !== null) {
    elements.settingsStatus.textContent = connected
      ? "Settings are locked for this connection. Disconnect to make changes."
      : "Changes apply to your next connection.";
  }
}

function applySettings(configuration) {
  elements.sttModel.value = configuration.stt_model;
  elements.ttsModel.value = configuration.tts_model;
  elements.ttsVoice.value = configuration.tts_voice;
  elements.speechLanguage.value = configuration.speech_language;
  elements.vadThreshold.value = String(configuration.vad_threshold);
  elements.endpointSilence.value = String(configuration.endpoint_silence_ms);
  elements.prefixAudio.value = String(configuration.prefix_ms);
  elements.minSpeech.value = String(configuration.min_speech_ms);
  elements.maxTurn.value = String(configuration.max_turn_seconds);
  elements.echoCancellation.checked = true;
  elements.noiseSuppression.checked = true;
  elements.autoGain.checked = true;
  updateSettingOutputs();
}

function updateSettingOutputs() {
  elements.vadThresholdValue.textContent = Number(elements.vadThreshold.value).toFixed(2);
}

function readSessionConfiguration() {
  const inputs = elements.voiceSettings.querySelectorAll("input");
  for (const input of inputs) {
    if (!input.checkValidity()) {
      input.reportValidity();
      throw new Error(`Check the ${input.closest("label")?.querySelector("span")?.textContent || "session"} setting.`);
    }
  }
  return {
    stt_model: elements.sttModel.value.trim(),
    tts_model: elements.ttsModel.value.trim(),
    tts_voice: elements.ttsVoice.value.trim(),
    speech_language: elements.speechLanguage.value.trim(),
    vad_threshold: Number(elements.vadThreshold.value),
    endpoint_silence_ms: Number(elements.endpointSilence.value),
    prefix_ms: Number(elements.prefixAudio.value),
    min_speech_ms: Number(elements.minSpeech.value),
    max_turn_seconds: Number(elements.maxTurn.value),
  };
}

async function loadSettings() {
  try {
    const response = await fetch("/dev/configuration");
    if (!response.ok) throw new Error(`settings request failed (${response.status})`);
    const payload = await response.json();
    serverDefaults = payload.defaults;
    applySettings(serverDefaults);
    setConnected(false);
    record("Server defaults loaded");
  } catch (error) {
    elements.settingsStatus.textContent = "Settings could not be loaded. Refresh to try again.";
    record(error.message);
  }
}

function addMessage(role, text, generationId = "") {
  elements.conversation.querySelector(".empty-conversation")?.remove();
  if (role === "assistant" && generationId && assistantMessages.has(generationId)) {
    const message = assistantMessages.get(generationId);
    message.textContent += text;
    elements.conversation.scrollTop = elements.conversation.scrollHeight;
    return;
  }
  const message = document.createElement("p");
  message.className = `message ${role}`;
  message.textContent = text;
  elements.conversation.append(message);
  elements.conversation.scrollTop = elements.conversation.scrollHeight;
  if (role === "assistant" && generationId) assistantMessages.set(generationId, message);
}

function handleControlMessage(message) {
  if (message.type === "state.changed") {
    elements.conversationState.textContent = message.state;
  } else if (message.type === "transcript.final") {
    addMessage("user", message.text);
  } else if (message.type === "assistant.text.delta") {
    addMessage("assistant", message.text, message.generation_id);
  } else if (message.type === "assistant.speaking") {
    void playRemoteAudio();
  } else if (message.type === "assistant.cancelled") {
    elements.remoteAudio.pause();
    elements.conversationState.textContent = "interrupted";
    const cancelled = assistantMessages.get(message.generation_id);
    if (cancelled) cancelled.dataset.interrupted = "true";
  } else if (message.type === "error") {
    elements.conversationState.textContent = `error · ${message.component || message.code}`;
  }
  record(`Fennec: ${message.type}`);
}

function microphoneConstraints(deviceId = "") {
  return {
    audio: {
      echoCancellation: elements.echoCancellation.checked,
      noiseSuppression: elements.noiseSuppression.checked,
      autoGainControl: elements.autoGain.checked,
      channelCount: 1,
      ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
    },
    video: false,
  };
}

async function gatherIceCandidates(connection) {
  if (connection.iceGatheringState === "complete") return;
  await new Promise((resolve) => {
    const listener = () => {
      if (connection.iceGatheringState === "complete") {
        connection.removeEventListener("icegatheringstatechange", listener);
        resolve();
      }
    };
    connection.addEventListener("icegatheringstatechange", listener);
  });
}

async function populateMicrophones(selectedId = "") {
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter(
    (device) => device.kind === "audioinput",
  );
  elements.microphone.replaceChildren();
  for (const [index, device] of devices.entries()) {
    const option = document.createElement("option");
    option.value = device.deviceId;
    option.textContent = device.label || `Microphone ${index + 1}`;
    option.selected = device.deviceId === selectedId;
    elements.microphone.append(option);
  }
}

function inspectTrack(track) {
  elements.diagnostics.textContent = JSON.stringify(
    { settings: track.getSettings(), constraints: track.getConstraints() },
    null,
    2,
  );
}

async function playRemoteAudio() {
  try {
    await elements.remoteAudio.play();
    elements.resumeAudio.classList.add("hidden");
  } catch (error) {
    elements.resumeAudio.classList.remove("hidden");
    record(`Browser paused received audio: ${error.message}`);
  }
}

async function connect() {
  setStatus("Requesting microphone…");
  elements.connect.disabled = true;
  elements.voiceSettings.disabled = true;
  elements.resetSettings.disabled = true;
  elements.settingsStatus.textContent = "Preparing this connection with your settings…";
  try {
    if (serverDefaults === null) throw new Error("Wait for the server settings to load.");
    const configuration = readSessionConfiguration();
    microphoneStream = await navigator.mediaDevices.getUserMedia(microphoneConstraints());
    const microphoneTrack = microphoneStream.getAudioTracks()[0];
    inspectTrack(microphoneTrack);
    await populateMicrophones(microphoneTrack.getSettings().deviceId);

    const response = await fetch("/dev/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ configuration }),
    });
    if (!response.ok) {
      const failure = await response.json().catch(() => ({}));
      throw new Error(failure.detail || `session request failed (${response.status})`);
    }
    session = await response.json();

    peer = new RTCPeerConnection();
    control = peer.createDataChannel("fennec-control", { ordered: true });
    peer.addTrack(microphoneTrack, microphoneStream);

    control.addEventListener("open", () => {
      elements.tone.disabled = false;
      record("Control channel ready");
    });
    control.addEventListener("message", (event) => {
      try {
        const message = JSON.parse(event.data);
        handleControlMessage(message);
      } catch {
        record("Fennec sent an unreadable control event");
      }
    });
    peer.addEventListener("track", (event) => {
      elements.remoteAudio.srcObject = event.streams[0] || new MediaStream([event.track]);
      void playRemoteAudio();
      record("Assistant audio track received");
    });
    peer.addEventListener("connectionstatechange", () => {
      const state = peer?.connectionState || "closed";
      setStatus(state === "connected" ? "Connected — microphone is live" : state, state);
      record(`Peer connection: ${state}`);
      if (["failed", "closed"].includes(state)) setConnected(false);
    });

    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    await gatherIceCandidates(peer);
    const answerResponse = await fetch(session.signaling_url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${session.access_token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(peer.localDescription),
    });
    if (!answerResponse.ok) throw new Error(`signaling failed (${answerResponse.status})`);
    await peer.setRemoteDescription(await answerResponse.json());
    setConnected(true);
  } catch (error) {
    record(error.message);
    setStatus("Connection failed", "failed");
    await disconnect(false);
  }
}

async function disconnect(updateStatus = true) {
  if (control?.readyState === "open") {
    control.send(JSON.stringify({ type: "session.close" }));
  }
  control?.close();
  peer?.close();
  microphoneStream?.getTracks().forEach((track) => track.stop());
  elements.remoteAudio.srcObject = null;
  peer = null;
  control = null;
  microphoneStream = null;
  session = null;
  elements.mute.textContent = "Mute";
  elements.resumeAudio.classList.add("hidden");
  elements.conversationState.textContent = "Waiting to connect";
  setConnected(false);
  elements.connect.disabled = false;
  if (updateStatus) setStatus("Disconnected");
}

elements.connect.addEventListener("click", () => void connect());
elements.disconnect.addEventListener("click", () => void disconnect());
elements.mute.addEventListener("click", () => {
  const track = microphoneStream?.getAudioTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  elements.mute.textContent = track.enabled ? "Mute" : "Unmute";
  record(track.enabled ? "Microphone unmuted" : "Microphone muted");
});
elements.tone.addEventListener("click", () => {
  if (control?.readyState === "open") control.send(JSON.stringify({ type: "audio.check" }));
});
elements.resumeAudio.addEventListener("click", () => void playRemoteAudio());
elements.resetSettings.addEventListener("click", () => {
  if (serverDefaults === null) return;
  applySettings(serverDefaults);
  elements.settingsStatus.textContent = "Server defaults restored for the next connection.";
  record("Session settings reset to server defaults");
});
elements.vadThreshold.addEventListener("input", updateSettingOutputs);
elements.microphone.addEventListener("change", async () => {
  const sender = peer?.getSenders().find((candidate) => candidate.track?.kind === "audio");
  if (!sender) return;
  try {
    const nextStream = await navigator.mediaDevices.getUserMedia(
      microphoneConstraints(elements.microphone.value),
    );
    const nextTrack = nextStream.getAudioTracks()[0];
    await sender.replaceTrack(nextTrack);
    microphoneStream?.getTracks().forEach((track) => track.stop());
    microphoneStream = nextStream;
    inspectTrack(nextTrack);
    record("Microphone changed");
  } catch (error) {
    record(`Microphone change failed: ${error.message}`);
  }
});
window.addEventListener("beforeunload", () => void disconnect(false));
void loadSettings();
