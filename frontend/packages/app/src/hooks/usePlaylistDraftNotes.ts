import { useQuery } from '@tanstack/react-query';
import { DraftNote, type EntityId } from '@dna/core';
import { apiHandler } from '../api';

export function usePlaylistDraftNotes(
    playlistId?: EntityId | null
) {
    const isEnabled = playlistId != null;

    return useQuery<DraftNote[], Error>({
        queryKey: ['draftNotes', playlistId],
        queryFn: () =>
            apiHandler.getPlaylistDraftNotes(playlistId!),
        enabled: isEnabled,
        staleTime: 1000 * 60, // 1 minute
    });
}
