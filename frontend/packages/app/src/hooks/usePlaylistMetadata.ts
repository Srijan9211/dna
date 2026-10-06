import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { PlaylistMetadata, PlaylistMetadataUpdate } from '@dna/core';
import { apiHandler } from '../api';
import type { EntityId } from '@dna/core';

export function usePlaylistMetadata(playlistId: EntityId | null) {
  return useQuery<PlaylistMetadata | null, Error>({
    queryKey: ['playlistMetadata', playlistId],
    queryFn: () => apiHandler.getPlaylistMetadata({ playlistId: playlistId! }),
    enabled: !!playlistId,
  });
}

export function useUpsertPlaylistMetadata(playlistId: EntityId | null) {
  const queryClient = useQueryClient();

  return useMutation<PlaylistMetadata, Error, PlaylistMetadataUpdate>({
    mutationFn: (data: PlaylistMetadataUpdate) =>
      apiHandler.upsertPlaylistMetadata({ playlistId: playlistId!, data }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['playlistMetadata', playlistId],
      });
    },
  });
}

export function useSetInReview(playlistId: EntityId | null) {
  const mutation = useUpsertPlaylistMetadata(playlistId);

  const setInReview = (versionId: EntityId) => {
    return mutation.mutateAsync({ in_review: versionId });
  };

  return {
    setInReview,
    isLoading: mutation.isPending,
    error: mutation.error,
  };
}
