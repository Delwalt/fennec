import type { FennecTranscript } from './types.ts';

export function retainTranscript(
  transcripts: Map<string, FennecTranscript>,
  transcript: FennecTranscript,
  limit: number,
): void {
  transcripts.set(transcript.id, transcript);
  while (transcripts.size > limit) {
    const oldestId = transcripts.keys().next().value;
    if (oldestId === undefined) return;
    transcripts.delete(oldestId);
  }
}

export function mergeConsecutiveTranscripts(
  transcripts: FennecTranscript[],
): FennecTranscript[] {
  const conversation: FennecTranscript[] = [];

  for (const transcript of transcripts) {
    if (!transcript.text.trim()) continue;

    const previous = conversation.at(-1);
    if (!previous || previous.speaker !== transcript.speaker) {
      conversation.push({ ...transcript });
      continue;
    }

    previous.text = joinSpokenFragments(previous.text, transcript.text);
    previous.isFinal = previous.isFinal && transcript.isFinal;
  }

  return conversation;
}

function joinSpokenFragments(first: string, second: string): string {
  const next = second.trimStart();
  const separator = /^[,.:;!?)]/.test(next) ? '' : ' ';
  return `${first.trimEnd()}${separator}${next}`;
}
