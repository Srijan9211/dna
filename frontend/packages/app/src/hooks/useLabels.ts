import { useCallback } from 'react';
import { useRuntimeConfig } from '../contexts/RuntimeConfigContext';
import { labelFor } from '../config/runtimeConfig';

/**
 * The tracker's own word for a DNA kind: `label('playlist')` is "Playlist" on
 * ShotGrid and "List" on AYON.  Use it for any user-facing text that names a tracker
 * concept, so studios see the vocabulary they already know.
 */
export function useLabels(): (kind: string) => string {
  const config = useRuntimeConfig();
  return useCallback((kind: string) => labelFor(config, kind), [config]);
}
