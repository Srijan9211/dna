/* eslint-disable react-refresh/only-export-components */
import {
  createContext,
  useContext,
  useState,
  useCallback,
  type ReactNode,
} from 'react';
import { useRuntimeConfig } from './RuntimeConfigContext';

const TRANSCRIPTION_KEY = 'dna-transcription-enabled';
const AI_KEY = 'dna-ai-enabled';
const IN_REVIEW_KEY = 'dna-in-review-enabled';

interface FeatureFlagsContextValue {
  transcriptionEnabled: boolean;
  aiEnabled: boolean;
  inReviewEnabled: boolean;
  transcriptionLocked: boolean;
  aiLocked: boolean;
  inReviewLocked: boolean;
  transcriptionLockReason: string | null;
  inReviewLockReason: string | null;
  setTranscriptionEnabled: (enabled: boolean) => void;
  setAiEnabled: (enabled: boolean) => void;
  setInReviewEnabled: (enabled: boolean) => void;
}

const FeatureFlagsContext = createContext<FeatureFlagsContextValue | null>(
  null
);

export function FeatureFlagsProvider({ children }: { children: ReactNode }) {
  // A deployment can pin a flag on or off for everyone ("pipeline" lock): from the
  // server's frontend settings, else the legacy VITE_FEATURE_* build variables.
  // null means no pin, so the user's own saved choice (or the default) applies.
  const { features } = useRuntimeConfig();
  const pinnedTranscription = features.transcription;
  const pinnedInReview = features.inReview;
  const pinnedAi = features.ai;

  const [transcriptionBase, setTranscriptionState] = useState(() => {
    if (pinnedTranscription !== null) return pinnedTranscription;
    const stored = localStorage.getItem(TRANSCRIPTION_KEY);
    return stored === null ? true : stored === 'true';
  });

  const [aiEnabled, setAiState] = useState(() => {
    if (pinnedAi !== null) return pinnedAi;
    const stored = localStorage.getItem(AI_KEY);
    return stored === null ? true : stored === 'true';
  });

  const [inReviewBase, setInReviewState] = useState(() => {
    if (pinnedInReview !== null) return pinnedInReview;
    const stored = localStorage.getItem(IN_REVIEW_KEY);
    return stored === null ? true : stored === 'true';
  });

  // Russian-doll dependency: AI ⊆ Transcription ⊆ In Review.
  // AI requires Transcription, and Transcription requires In Review, so
  // enabling a parent (via UI toggle or env override) forces its children on.
  const transcriptionEnabled = transcriptionBase || aiEnabled;
  const inReviewEnabled = inReviewBase || transcriptionEnabled;

  const setTranscriptionEnabled = useCallback(
    (enabled: boolean) => {
      if (pinnedTranscription !== null) return;
      localStorage.setItem(TRANSCRIPTION_KEY, String(enabled));
      setTranscriptionState(enabled);
    },
    [pinnedTranscription]
  );

  const setAiEnabled = useCallback(
    (enabled: boolean) => {
      if (pinnedAi !== null) return;
      localStorage.setItem(AI_KEY, String(enabled));
      setAiState(enabled);
    },
    [pinnedAi]
  );

  const setInReviewEnabled = useCallback(
    (enabled: boolean) => {
      if (pinnedInReview !== null) return;
      localStorage.setItem(IN_REVIEW_KEY, String(enabled));
      setInReviewState(enabled);
    },
    [pinnedInReview]
  );

  return (
    <FeatureFlagsContext.Provider
      value={{
        transcriptionEnabled,
        aiEnabled,
        inReviewEnabled,
        transcriptionLocked: pinnedTranscription !== null || aiEnabled,
        aiLocked: pinnedAi !== null,
        inReviewLocked: pinnedInReview !== null || transcriptionEnabled,
        transcriptionLockReason:
          pinnedTranscription !== null ? 'pipeline' : aiEnabled ? 'ai' : null,
        inReviewLockReason:
          pinnedInReview !== null
            ? 'pipeline'
            : transcriptionEnabled
              ? 'transcription'
              : null,
        setTranscriptionEnabled,
        setAiEnabled,
        setInReviewEnabled,
      }}
    >
      {children}
    </FeatureFlagsContext.Provider>
  );
}

export function useFeatureFlags() {
  const ctx = useContext(FeatureFlagsContext);
  if (!ctx)
    throw new Error('useFeatureFlags must be used within FeatureFlagsProvider');
  return ctx;
}
