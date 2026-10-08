import {
  ApiHandler,
  Playlist,
  Project,
  User,
  Version,
  type ConfigAccess,
  type ConfigRevision,
  type EffectiveConfig,
  type EntityId,
  type ProjectOverrides,
} from '@dna/core';
import { useQuery } from '@tanstack/react-query';

const apiHandler = new ApiHandler({
  baseURL: import.meta.env.VITE_API_BASE_URL,
});

function useGetProjectsForUser(userEmail: string | null) {
  return useQuery<Project[], Error>({
    queryKey: ['projects', userEmail],
    queryFn: () => apiHandler.getProjectsForUser({ userEmail: userEmail! }),
    enabled: !!userEmail,
  });
}

function useGetPlaylistsForProject(projectId: EntityId | null) {
  return useQuery<Playlist[], Error>({
    queryKey: ['playlists', projectId],
    queryFn: () => apiHandler.getPlaylistsForProject({ projectId: projectId! }),
    enabled: !!projectId,
  });
}

function useGetVersionsForPlaylist(playlistId: EntityId | null) {
  return useQuery<Version[], Error>({
    queryKey: ['versions', playlistId],
    queryFn: () =>
      apiHandler.getVersionsForPlaylist({ playlistId: playlistId! }),
    enabled: !!playlistId,
  });
}

function useGetUserByEmail(userEmail: string | null) {
  return useQuery<User, Error>({
    queryKey: ['user', userEmail],
    queryFn: () => apiHandler.getUserByEmail({ userEmail: userEmail! }),
    enabled: !!userEmail,
  });
}

/**
 * Whether the signed-in user may open the admin pages.  Any failure (not signed in,
 * admin API off, network) means "no": the pages are simply not offered.
 */
function useConfigAccess(enabled = true) {
  return useQuery<ConfigAccess, Error>({
    queryKey: ['config', 'access'],
    queryFn: async () => {
      try {
        return await apiHandler.getConfigAccess();
      } catch {
        return { admin: false };
      }
    },
    enabled,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
}

function useEffectiveConfig(enabled = true) {
  return useQuery<EffectiveConfig, Error>({
    queryKey: ['config', 'effective'],
    queryFn: () => apiHandler.getEffectiveConfig(),
    enabled,
  });
}

function useConfigHistory(enabled = true) {
  return useQuery<ConfigRevision[], Error>({
    queryKey: ['config', 'history'],
    queryFn: () => apiHandler.getConfigHistory(50),
    enabled,
  });
}

function useProjectOverrides(enabled = true) {
  return useQuery<ProjectOverrides, Error>({
    queryKey: ['config', 'projects'],
    queryFn: () => apiHandler.getProjectOverrides(),
    enabled,
  });
}

export {
  useProjectOverrides,
  useConfigAccess,
  useEffectiveConfig,
  useConfigHistory,
  useGetProjectsForUser,
  useGetPlaylistsForProject,
  useGetVersionsForPlaylist,
  useGetUserByEmail,
  apiHandler,
};
