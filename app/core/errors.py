class PixivRemoteError(Exception):
    status_code = 502


class PixivNotFoundError(PixivRemoteError):
    status_code = 404


class PixivRestrictedError(PixivRemoteError):
    status_code = 403


class PixivAuthError(PixivRemoteError):
    status_code = 401


class PixivUnavailableError(PixivRemoteError):
    status_code = 503


class ArchiveStorageError(PixivRemoteError):
    status_code = 507


class ArchiveMaintenanceError(PixivRemoteError):
    status_code = 503
