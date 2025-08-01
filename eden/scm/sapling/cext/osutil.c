/*
 * Portions Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This software may be used and distributed according to the terms of the
 * GNU General Public License version 2.
 */

/*
 osutil.c - native operating system services

 Copyright 2007 Olivia Mackall and others

 This software may be used and distributed according to the terms of
 the GNU General Public License, incorporated herein by reference.
*/

#define _ATFILE_SOURCE
#define PY_SSIZE_T_CLEAN
#include <Python.h> // @manual=fbsource//third-party/python:python
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <io.h> // @manual
#include <windows.h>
#else
#include <dirent.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#endif

#ifdef __APPLE__
#include <crt_externs.h> // @manual
#include <sys/attr.h> // @manual
#include <sys/vnode.h> // @manual
#endif

#include "eden/scm/sapling/cext/util.h"

/* some platforms lack the PATH_MAX definition (eg. GNU/Hurd) */
#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#ifdef _WIN32
/*
stat struct compatible with hg expectations
Mercurial only uses st_mode, st_size and st_mtime
the rest is kept to minimize changes between implementations
*/
struct hg_stat {
  int st_dev;
  int st_mode;
  int st_nlink;
  __int64 st_size;
  int st_mtime;
  int st_ctime;
};
struct listdir_stat {
  PyObject_HEAD struct hg_stat st;
};
#else
struct listdir_stat {
  PyObject_HEAD struct stat st;
};
#endif

#ifdef IS_PY3K
#define listdir_slot(name)                                         \
  static PyObject* listdir_stat_##name(PyObject* self, void* x) {  \
    return PyLong_FromLong(((struct listdir_stat*)self)->st.name); \
  }
#else
#define listdir_slot(name)                                        \
  static PyObject* listdir_stat_##name(PyObject* self, void* x) { \
    return PyInt_FromLong(((struct listdir_stat*)self)->st.name); \
  }
#endif

listdir_slot(st_dev) listdir_slot(st_mode) listdir_slot(st_nlink)
#ifdef _WIN32
    static PyObject* listdir_stat_st_size(PyObject* self, void* x) {
  return PyLong_FromLongLong(
      (PY_LONG_LONG)((struct listdir_stat*)self)->st.st_size);
}
#else
    listdir_slot(st_size)
#endif
listdir_slot(st_mtime) listdir_slot(st_ctime)

    static struct PyGetSetDef listdir_stat_getsets[] = {
        {"st_dev", listdir_stat_st_dev, 0, 0, 0},
        {"st_mode", listdir_stat_st_mode, 0, 0, 0},
        {"st_nlink", listdir_stat_st_nlink, 0, 0, 0},
        {"st_size", listdir_stat_st_size, 0, 0, 0},
        {"st_mtime", listdir_stat_st_mtime, 0, 0, 0},
        {"st_ctime", listdir_stat_st_ctime, 0, 0, 0},
        {0, 0, 0, 0, 0}};

static PyObject* listdir_stat_new(PyTypeObject* t, PyObject* a, PyObject* k) {
  return t->tp_alloc(t, 0);
}

static void listdir_stat_dealloc(PyObject* o) {
  o->ob_type->tp_free(o);
}

static PyTypeObject listdir_stat_type = {
    PyVarObject_HEAD_INIT(NULL, 0) /* header */
    "osutil.stat", /*tp_name*/
    sizeof(struct listdir_stat), /*tp_basicsize*/
    0, /*tp_itemsize*/
    (destructor)listdir_stat_dealloc, /*tp_dealloc*/
    0, /*tp_print*/
    0, /*tp_getattr*/
    0, /*tp_setattr*/
    0, /*tp_compare*/
    0, /*tp_repr*/
    0, /*tp_as_number*/
    0, /*tp_as_sequence*/
    0, /*tp_as_mapping*/
    0, /*tp_hash */
    0, /*tp_call*/
    0, /*tp_str*/
    0, /*tp_getattro*/
    0, /*tp_setattro*/
    0, /*tp_as_buffer*/
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE, /*tp_flags*/
    "stat objects", /* tp_doc */
    0, /* tp_traverse */
    0, /* tp_clear */
    0, /* tp_richcompare */
    0, /* tp_weaklistoffset */
    0, /* tp_iter */
    0, /* tp_iternext */
    0, /* tp_methods */
    0, /* tp_members */
    listdir_stat_getsets, /* tp_getset */
    0, /* tp_base */
    0, /* tp_dict */
    0, /* tp_descr_get */
    0, /* tp_descr_set */
    0, /* tp_dictoffset */
    0, /* tp_init */
    0, /* tp_alloc */
    listdir_stat_new, /* tp_new */
};

#ifdef _WIN32

static int to_python_time(const FILETIME* tm) {
  /* number of seconds between epoch and January 1 1601 */
  const __int64 a0 = (__int64)134774L * (__int64)24L * (__int64)3600L;
  /* conversion factor from 100ns to 1s */
  const __int64 a1 = 10000000;
  /* explicit (int) cast to suspend compiler warnings */
  return (int)((((__int64)tm->dwHighDateTime << 32) + tm->dwLowDateTime) / a1 -
               a0);
}

#ifdef IS_PY3K

static PyObject* make_item(const WIN32_FIND_DATAW* fd, int wantstat) {
  PyObject* py_st;
  struct hg_stat* stp;

  int kind = (fd->dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
      ? ((fd->dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) ? S_IFLNK
                                                               : _S_IFDIR)
      : _S_IFREG;
  if (!wantstat) {
    PyObject* unicode =
        PyUnicode_FromWideChar(fd->cFileName, wcslen(fd->cFileName));
    if (!unicode) {
      return NULL;
    }
    return Py_BuildValue("Ni", unicode, kind);
  }

  py_st = PyObject_CallObject((PyObject*)&listdir_stat_type, NULL);
  if (!py_st)
    return NULL;

  stp = &((struct listdir_stat*)py_st)->st;
  /*
  use kind as st_mode
  rwx bits on Win32 are meaningless
  and Hg does not use them anyway
  */
  stp->st_mode = kind;
  stp->st_mtime = to_python_time(&fd->ftLastWriteTime);
  stp->st_ctime = to_python_time(&fd->ftCreationTime);
  if (kind == _S_IFREG)
    stp->st_size = ((__int64)fd->nFileSizeHigh << 32) + fd->nFileSizeLow;

  PyObject* unicode =
      PyUnicode_FromWideChar(fd->cFileName, wcslen(fd->cFileName));
  if (!unicode) {
    return NULL;
  }

  return Py_BuildValue("NiN", unicode, kind, py_st);
}

static PyObject* _listdir(
    const wchar_t* path,
    Py_ssize_t plen,
    int wantstat,
    const wchar_t* skip) {
  PyObject* rval = NULL; /* initialize - return value */
  PyObject* list;
  HANDLE fh;
  WIN32_FIND_DATAW fd;

  /* build the path + \* pattern string */
  wchar_t* pattern;
  pattern =
      PyMem_Malloc((plen + 3) * sizeof(wchar_t)); /* wchar_path + \* + \0 */
  if (!pattern) {
    PyErr_NoMemory();
    goto error_nomem;
  }
  memcpy(pattern, path, plen * sizeof(wchar_t));

  if (plen > 0) {
    wchar_t c = path[plen - 1];
    if (c != L':' && c != L'/' && c != L'\\')
      pattern[plen++] = L'\\';
  }
  pattern[plen++] = L'*';
  pattern[plen] = L'\0';

  fh = FindFirstFileW(pattern, &fd);
  if (fh == INVALID_HANDLE_VALUE) {
    PyErr_SetFromWindowsErrWithFilename(GetLastError(), path);
    goto error_file;
  }

  list = PyList_New(0);
  if (!list)
    goto error_list;

  do {
    PyObject* item;

    if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
      if (!wcscmp(fd.cFileName, L".") || !wcscmp(fd.cFileName, L".."))
        continue;

      if (skip && !wcscmp(fd.cFileName, skip)) {
        rval = PyList_New(0);
        goto error;
      }
    }

    item = make_item(&fd, wantstat);
    if (!item) {
      goto error;
    }

    if (PyList_Append(list, item)) {
      Py_XDECREF(item);
      goto error;
    }

    Py_XDECREF(item);
  } while (FindNextFileW(fh, &fd));

  if (GetLastError() != ERROR_NO_MORE_FILES) {
    PyErr_SetFromWindowsErrWithFilename(GetLastError(), path);
    goto error;
  }

  rval = list;
  Py_XINCREF(rval);
error:
  Py_XDECREF(list);
error_list:
  FindClose(fh);
error_file:
  PyMem_Free(pattern);
error_nomem:
  return rval;
}

// Else is python 2
#else

static PyObject* make_item(const WIN32_FIND_DATAA* fd, int wantstat) {
  PyObject* py_st;
  struct hg_stat* stp;
#ifndef IS_PY3K
  const int S_IFLNK = 40960; /* stat.S_IFLINK defined by Python on Windows */
#endif

  int kind = (fd->dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
      ? ((fd->dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) ? S_IFLNK
                                                               : _S_IFDIR)
      : _S_IFREG;

  if (!wantstat)
    return Py_BuildValue("si", fd->cFileName, kind);

  py_st = PyObject_CallObject((PyObject*)&listdir_stat_type, NULL);
  if (!py_st)
    return NULL;

  stp = &((struct listdir_stat*)py_st)->st;
  /*
  use kind as st_mode
  rwx bits on Win32 are meaningless
  and Hg does not use them anyway
  */
  stp->st_mode = kind;
  stp->st_mtime = to_python_time(&fd->ftLastWriteTime);
  stp->st_ctime = to_python_time(&fd->ftCreationTime);
  if (kind == _S_IFREG)
    stp->st_size = ((__int64)fd->nFileSizeHigh << 32) + fd->nFileSizeLow;
  return Py_BuildValue("siN", fd->cFileName, kind, py_st);
}

static PyObject*
_listdir(const char* path, Py_ssize_t plen, int wantstat, const char* skip) {
  PyObject* rval = NULL; /* initialize - return value */
  PyObject* list;
  HANDLE fh;
  WIN32_FIND_DATAA fd;
  char* pattern;

  /* build the path + \* pattern string */
  pattern = PyMem_Malloc(plen + 3); /* path + \* + \0 */
  if (!pattern) {
    PyErr_NoMemory();
    goto error_nomem;
  }
  memcpy(pattern, path, plen);

  if (plen > 0) {
    char c = path[plen - 1];
    if (c != ':' && c != '/' && c != '\\')
      pattern[plen++] = '\\';
  }
  pattern[plen++] = '*';
  pattern[plen] = '\0';

  fh = FindFirstFileA(pattern, &fd);
  if (fh == INVALID_HANDLE_VALUE) {
    PyErr_SetFromWindowsErrWithFilename(GetLastError(), path);
    goto error_file;
  }

  list = PyList_New(0);
  if (!list)
    goto error_list;

  do {
    PyObject* item;

    if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
      if (!strcmp(fd.cFileName, ".") || !strcmp(fd.cFileName, ".."))
        continue;

      if (skip && !strcmp(fd.cFileName, skip)) {
        rval = PyList_New(0);
        goto error;
      }
    }

    item = make_item(&fd, wantstat);
    if (!item)
      goto error;

    if (PyList_Append(list, item)) {
      Py_XDECREF(item);
      goto error;
    }

    Py_XDECREF(item);
  } while (FindNextFileA(fh, &fd));

  if (GetLastError() != ERROR_NO_MORE_FILES) {
    PyErr_SetFromWindowsErrWithFilename(GetLastError(), path);
    goto error;
  }

  rval = list;
  Py_XINCREF(rval);
error:
  Py_XDECREF(list);
error_list:
  FindClose(fh);
error_file:
  PyMem_Free(pattern);
error_nomem:
  return rval;
}

#endif

#else

int entkind(struct dirent* ent) {
#ifdef DT_REG
  switch (ent->d_type) {
    case DT_REG:
      return S_IFREG;
    case DT_DIR:
      return S_IFDIR;
    case DT_LNK:
      return S_IFLNK;
    case DT_BLK:
      return S_IFBLK;
    case DT_CHR:
      return S_IFCHR;
    case DT_FIFO:
      return S_IFIFO;
    case DT_SOCK:
      return S_IFSOCK;
  }
#endif
  return -1;
}

static PyObject* makestat(const struct stat* st) {
  PyObject* stat;

  stat = PyObject_CallObject((PyObject*)&listdir_stat_type, NULL);
  if (stat) {
    memcpy(&((struct listdir_stat*)stat)->st, st, sizeof(*st));
  }
  return stat;
}

static PyObject*
_listdir_stat(const char* path, int pathlen, int keepstat, const char* skip) {
  PyObject *list, *elem, *stat = NULL, *ret = NULL;
  char fullpath[PATH_MAX + 10];
  int kind, err;
  struct stat st;
  struct dirent* ent;
  DIR* dir;
#ifdef AT_SYMLINK_NOFOLLOW
  int dfd = -1;
#endif

  if (pathlen >= PATH_MAX) {
    errno = ENAMETOOLONG;
    PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
    goto error_value;
  }
  strncpy(fullpath, path, PATH_MAX);
  fullpath[pathlen] = '/';

#ifdef AT_SYMLINK_NOFOLLOW
  dfd = open(path, O_RDONLY);
  if (dfd == -1) {
    PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
    goto error_value;
  }
  dir = fdopendir(dfd);
#else
  dir = opendir(path);
#endif
  if (!dir) {
    PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
    goto error_dir;
  }

  list = PyList_New(0);
  if (!list) {
    goto error_list;
  }

  while ((ent = readdir(dir))) {
    if (!strcmp(ent->d_name, ".") || !strcmp(ent->d_name, "..")) {
      continue;
    }

    kind = entkind(ent);
    if (kind == -1 || keepstat) {
#ifdef AT_SYMLINK_NOFOLLOW
      err = fstatat(dfd, ent->d_name, &st, AT_SYMLINK_NOFOLLOW);
#else
      strncpy(fullpath + pathlen + 1, ent->d_name, PATH_MAX - pathlen);
      fullpath[PATH_MAX] = '\0';
      err = lstat(fullpath, &st);
#endif
      if (err == -1) {
        /* race with file deletion? */
        if (errno == ENOENT) {
          continue;
        }
        strncpy(fullpath + pathlen + 1, ent->d_name, PATH_MAX - pathlen);
        fullpath[PATH_MAX] = 0;
        PyErr_SetFromErrnoWithFilename(PyExc_OSError, fullpath);
        goto error;
      }
      kind = st.st_mode & S_IFMT;
    }

    /* quit early? */
    if (skip && kind == S_IFDIR && !strcmp(ent->d_name, skip)) {
      ret = PyList_New(0);
      goto error;
    }

    if (keepstat) {
      stat = makestat(&st);
      if (!stat) {
        goto error;
      }
      elem = Py_BuildValue("siN", ent->d_name, kind, stat);
    } else {
      elem = Py_BuildValue("si", ent->d_name, kind);
    }
    if (!elem) {
      goto error;
    }
    stat = NULL;

    PyList_Append(list, elem);
    Py_DECREF(elem);
  }

  ret = list;
  Py_INCREF(ret);

error:
  Py_DECREF(list);
  Py_XDECREF(stat);
error_list:
  closedir(dir);
  /* closedir also closes its dirfd */
  goto error_value;
error_dir:
#ifdef AT_SYMLINK_NOFOLLOW
  close(dfd);
#endif
error_value:
  return ret;
}

#ifdef __APPLE__

typedef struct {
  u_int32_t length;
  attrreference_t name;
  fsobj_type_t obj_type;
  struct timespec mtime;
#if __LITTLE_ENDIAN__
  mode_t access_mask;
  uint16_t padding;
#else
  uint16_t padding;
  mode_t access_mask;
#endif
  off_t size;
} __attribute__((packed)) attrbuf_entry;

int attrkind(attrbuf_entry* entry) {
  switch (entry->obj_type) {
    case VREG:
      return S_IFREG;
    case VDIR:
      return S_IFDIR;
    case VLNK:
      return S_IFLNK;
    case VBLK:
      return S_IFBLK;
    case VCHR:
      return S_IFCHR;
    case VFIFO:
      return S_IFIFO;
    case VSOCK:
      return S_IFSOCK;
  }
  return -1;
}

/* get these many entries at a time */
#define LISTDIR_BATCH_SIZE 50

static PyObject* _listdir_batch(
    const char* path,
    int pathlen,
    int keepstat,
    const char* skip,
    bool* fallback) {
  PyObject *list, *elem, *stat = NULL, *ret = NULL;
  int kind, err;
  unsigned long index;
  unsigned int count, old_state, new_state;
  bool state_seen = false;
  attrbuf_entry* entry;
  /* from the getattrlist(2) man page: a path can be no longer than
     (NAME_MAX * 3 + 1) bytes. Also, "The getattrlist() function will
     silently truncate attribute data if attrBufSize is too small." So
     pass in a buffer big enough for the worst case. */
  char attrbuf[LISTDIR_BATCH_SIZE * (sizeof(attrbuf_entry) + NAME_MAX * 3 + 1)];
  unsigned int basep_unused;

  struct stat st;
  int dfd = -1;

  /* these must match the attrbuf_entry struct, otherwise you'll end up
     with garbage */
  struct attrlist requested_attr = {0};
  requested_attr.bitmapcount = ATTR_BIT_MAP_COUNT;
  requested_attr.commonattr =
      (ATTR_CMN_NAME | ATTR_CMN_OBJTYPE | ATTR_CMN_MODTIME |
       ATTR_CMN_ACCESSMASK);
  requested_attr.fileattr = ATTR_FILE_DATALENGTH;

  *fallback = false;

  if (pathlen >= PATH_MAX) {
    errno = ENAMETOOLONG;
    PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
    goto error_value;
  }

  dfd = open(path, O_RDONLY);
  if (dfd == -1) {
    PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
    goto error_value;
  }

  list = PyList_New(0);
  if (!list)
    goto error_dir;

  do {
    count = LISTDIR_BATCH_SIZE;
    err = getdirentriesattr(
        dfd,
        &requested_attr,
        &attrbuf,
        sizeof(attrbuf),
        &count,
        &basep_unused,
        &new_state,
        0);
    if (err < 0) {
      if (errno == ENOTSUP) {
        /* We're on a filesystem that doesn't support
           getdirentriesattr. Fall back to the
           stat-based implementation. */
        *fallback = true;
      } else
        PyErr_SetFromErrnoWithFilename(PyExc_OSError, path);
      goto error;
    }

    if (!state_seen) {
      old_state = new_state;
      state_seen = true;
    } else if (old_state != new_state) {
      /* There's an edge case with getdirentriesattr. Consider
         the following initial list of files:

         a
         b
         <--
         c
         d

         If the iteration is paused at the arrow, and b is
         deleted before it is resumed, getdirentriesattr will
         not return d at all!  Ordinarily we're expected to
         restart the iteration from the beginning. To avoid
         getting stuck in a retry loop here, fall back to
         stat. */
      *fallback = true;
      goto error;
    }

    entry = (attrbuf_entry*)attrbuf;

    for (index = 0; index < count; index++) {
      char* filename = ((char*)&entry->name) + entry->name.attr_dataoffset;

      if (!strcmp(filename, ".") || !strcmp(filename, ".."))
        continue;

      kind = attrkind(entry);
      if (kind == -1) {
        PyErr_Format(
            PyExc_OSError,
            "unknown object type %u for file "
            "%s%s!",
            entry->obj_type,
            path,
            filename);
        goto error;
      }

      /* quit early? */
      if (skip && kind == S_IFDIR && !strcmp(filename, skip)) {
        ret = PyList_New(0);
        goto error;
      }

      if (keepstat) {
        /* from the getattrlist(2) man page: "Only the
           permission bits ... are valid". */
        st.st_mode = (entry->access_mask & ~S_IFMT) | kind;
        st.st_mtime = entry->mtime.tv_sec;
        st.st_size = entry->size;
        stat = makestat(&st);
        if (!stat)
          goto error;
        elem = Py_BuildValue("siN", filename, kind, stat);
      } else
        elem = Py_BuildValue("si", filename, kind);
      if (!elem)
        goto error;
      stat = NULL;

      PyList_Append(list, elem);
      Py_DECREF(elem);

      entry = (attrbuf_entry*)((char*)entry + entry->length);
    }
  } while (err == 0);

  ret = list;
  Py_INCREF(ret);

error:
  Py_DECREF(list);
  Py_XDECREF(stat);
error_dir:
  close(dfd);
error_value:
  return ret;
}

#endif /* __APPLE__ */

static PyObject*
_listdir(const char* path, int pathlen, int keepstat, const char* skip) {
#ifdef __APPLE__
  PyObject* ret;
  bool fallback = false;

  ret = _listdir_batch(path, pathlen, keepstat, skip, &fallback);
  if (ret != NULL || !fallback)
    return ret;
#endif
  return _listdir_stat(path, pathlen, keepstat, skip);
}

static PyObject* statfiles(PyObject* self, PyObject* args) {
  PyObject *names, *stats;
  Py_ssize_t i, count;

  if (!PyArg_ParseTuple(args, "O:statfiles", &names)) {
    return NULL;
  }

  count = PySequence_Length(names);
  if (count == -1) {
    PyErr_SetString(PyExc_TypeError, "not a sequence");
    return NULL;
  }

  stats = PyList_New(count);
  if (stats == NULL) {
    return NULL;
  }

  for (i = 0; i < count; i++) {
    PyObject *stat, *pypath;
    struct stat st;
    int ret, kind;
    const char* path;

    /* With a large file count or on a slow filesystem,
       don't block signals for long (issue4878). */
    if ((i % 1000) == 999 && PyErr_CheckSignals() == -1) {
      goto bail;
    }

    pypath = PySequence_GetItem(names, i);
    if (!pypath) {
      goto bail;
    }
#ifdef IS_PY3K
    path = PyUnicode_AsUTF8(pypath);
#else
    path = PyBytes_AsString(pypath);
#endif
    if (path == NULL) {
      Py_DECREF(pypath);
      PyErr_SetString(PyExc_TypeError, "not a str");
      goto bail;
    }
    ret = lstat(path, &st);
    Py_DECREF(pypath);
    kind = st.st_mode & S_IFMT;
    if (ret != -1 && (kind == S_IFREG || kind == S_IFLNK)) {
      stat = makestat(&st);
      if (stat == NULL) {
        goto bail;
      }
      PyList_SET_ITEM(stats, i, stat);
    } else {
      Py_INCREF(Py_None);
      PyList_SET_ITEM(stats, i, Py_None);
    }
  }

  return stats;

bail:
  Py_DECREF(stats);
  return NULL;
}

/*
 * recvfds() simply does not release GIL during blocking io operation because
 * command server is known to be single-threaded.
 *
 * Old systems such as Solaris don't provide CMSG_LEN, msg_control, etc.
 * Currently, recvfds() is not supported on these platforms.
 */
#ifdef CMSG_LEN

static ssize_t
recvfdstobuf(int sockfd, int** rfds, void* cbuf, size_t cbufsize) {
  char dummy[1];
  struct iovec iov = {dummy, sizeof(dummy)};
  struct msghdr msgh = {0};
  struct cmsghdr* cmsg;

  msgh.msg_iov = &iov;
  msgh.msg_iovlen = 1;
  msgh.msg_control = cbuf;
  msgh.msg_controllen = (socklen_t)cbufsize;
  if (recvmsg(sockfd, &msgh, 0) < 0) {
    return -1;
  }

  for (cmsg = CMSG_FIRSTHDR(&msgh); cmsg; cmsg = CMSG_NXTHDR(&msgh, cmsg)) {
    if (cmsg->cmsg_level != SOL_SOCKET || cmsg->cmsg_type != SCM_RIGHTS) {
      continue;
    }
    *rfds = (int*)CMSG_DATA(cmsg);
    return (cmsg->cmsg_len - CMSG_LEN(0)) / sizeof(int);
  }

  *rfds = cbuf;
  return 0;
}

static PyObject* recvfds(PyObject* self, PyObject* args) {
  int sockfd;
  int* rfds = NULL;
  ssize_t rfdscount, i;
  char cbuf[256];
  PyObject* rfdslist = NULL;

  if (!PyArg_ParseTuple(args, "i", &sockfd)) {
    return NULL;
  }

  rfdscount = recvfdstobuf(sockfd, &rfds, cbuf, sizeof(cbuf));
  if (rfdscount < 0) {
    return PyErr_SetFromErrno(PyExc_OSError);
  }

  rfdslist = PyList_New(rfdscount);
  if (!rfdslist) {
    goto bail;
  }
  for (i = 0; i < rfdscount; i++) {
    PyObject* obj = PyLong_FromLong(rfds[i]);
    if (!obj) {
      goto bail;
    }
    PyList_SET_ITEM(rfdslist, i, obj);
  }
  return rfdslist;

bail:
  Py_XDECREF(rfdslist);
  return NULL;
}

#endif /* CMSG_LEN */

#if defined(HAVE_SETPROCTITLE)
/* setproctitle is the first choice - available in FreeBSD */
#define SETPROCNAME_USE_SETPROCTITLE
#elif (defined(__linux__) || defined(__APPLE__))
/* rewrite the argv buffer in place - works in Linux and OS X */
#define SETPROCNAME_USE_ARGVREWRITE
#else
#define SETPROCNAME_USE_NONE
#endif

#ifdef SETPROCNAME_USE_ARGVREWRITE

/* Find the start of argv buffer (argv[0]) and its size */
static void getarg0size(char** argstart, size_t* argsize) {
#ifdef __APPLE__
  /* osx: crt_externs keeps a copy of argc, argv */
  int argc = *_NSGetArgc();
  char** argv = *_NSGetArgv();
  char *argvend, *argvstart;
  size_t argvsize = 0;
  int i;
  argvend = argvstart = argv[0];
  for (i = 0; i < argc; ++i) {
    if (argv[i] > argvend || argv[i] < argvstart)
      break; /* not continuous */
    size_t len = strlen(argv[i]);
    argvend = argv[i] + len + 1 /* '\0' */;
  }
  if (argvend > argvstart) /* sanity check */
    argvsize = argvend - argvstart;
  *argstart = argvstart;
  *argsize = argvsize;
#else
  /* On Linux >= 3.5, the 48 and 49 column of "/proc/self/stat" is the argv
     start and end pointers. See "man 5 proc". */
  FILE* fp;
  unsigned long start = 0, end = 0;
  fp = fopen("/proc/self/stat", "r");
  if (fp) {
    /* Note: if "comm" name has rare chars like spaces, the fscanf will fail */
    if (fscanf(
            fp,
            "%*d %*s %*c %*d %*d %*d %*d %*d %*u %*lu %*lu %*lu %*lu %*lu %*lu %*ld"
            " %*ld %*ld %*ld %*ld %*ld %*llu %*lu %*ld %*lu %*lu %*lu %*lu %*lu %*lu"
            " %*lu %*lu %*lu %*lu %*lu %*lu %*lu %*d %*d %*u %*u %*llu %*lu %*ld %*lu"
            " %*lu %*lu %lu %lu",
            &start,
            &end) == 2) {
      *argstart = (char*)start;
      *argsize = end - start;
    }
    fclose(fp);
  }
#endif
}

#endif // def SETPROCNAME_USE_ARGVREWRITE

#ifndef SETPROCNAME_USE_NONE
static PyObject* setprocname(PyObject* self, PyObject* args) {
  const char* name = NULL;
  if (!PyArg_ParseTuple(args, "s", &name)) {
    return NULL;
  }

#if defined(SETPROCNAME_USE_SETPROCTITLE)
  setproctitle("%s", name);
#elif defined(SETPROCNAME_USE_ARGVREWRITE)
  {
    static char* argvstart = NULL;
    static size_t argvsize = 0;
    if (argvstart == NULL && argvsize == 0) {
      argvsize = 1; /* do not try to obtain arg0 again */
      getarg0size(&argvstart, &argvsize);
    }

    if (argvstart && argvsize > 1) {
      int n = snprintf(argvstart, argvsize, "%s", name);
      if (n >= 0 && (size_t)n < argvsize) {
        memset(argvstart + n, 0, argvsize - n);
      }
    }
  }
#endif

  Py_RETURN_NONE;
}
#endif /* ndef SETPROCNAME_USE_NONE */

static PyObject* unblocksignal(PyObject* self, PyObject* args) {
  int sig = 0;
  int r;
  if (!PyArg_ParseTuple(args, "i", &sig)) {
    return NULL;
  }
  sigset_t set;
  r = sigemptyset(&set);
  if (r != 0) {
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  r = sigaddset(&set, sig);
  if (r != 0) {
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  r = sigprocmask(SIG_UNBLOCK, &set, NULL);
  if (r != 0) {
    return PyErr_SetFromErrno(PyExc_OSError);
  }
  Py_RETURN_NONE;
}

#endif /* ndef _WIN32 */

static PyObject* listdir(PyObject* self, PyObject* args, PyObject* kwargs) {
  PyObject* statobj = NULL; /* initialize - optional arg */
  PyObject* pathobj = NULL; /* initialize - optional arg */
  PyObject* skipobj = NULL; /* initialize - optional arg */
#if defined IS_PY3K && defined _WIN32
  const wchar_t *path = NULL, *skip = NULL;
#else
  const char *path = NULL, *skip = NULL;
#endif
  Py_ssize_t plen;
  int wantstat;

  static char* kwlist[] = {"path", "stat", "skip", NULL};

  if (!PyArg_ParseTupleAndKeywords(
          args, kwargs, "O|OO:listdir", kwlist, &pathobj, &statobj, &skipobj)) {
    return NULL;
  }

  wantstat = statobj && PyObject_IsTrue(statobj);

#ifdef IS_PY3K
#ifdef _WIN32
  path = PyUnicode_AsWideCharString(pathobj, &plen);
#else
  path = PyUnicode_AsUTF8AndSize(pathobj, &plen);
#endif
#else
  PyBytes_AsStringAndSize(pathobj, &path, &plen);
#endif
  if (!path) {
    return NULL;
  }

  if (skipobj && skipobj != Py_None) {
#ifdef IS_PY3K
#ifdef _WIN32
    skip = PyUnicode_AsWideCharString(skipobj, NULL);
#else
    skip = PyUnicode_AsUTF8(skipobj);
#endif
#else
    skip = PyBytes_AsString(skipobj);
#endif
    if (!skip) {
      return NULL;
    }
  }

  PyObject* result = _listdir(path, plen, wantstat, skip);
#ifdef IS_PY3K
#ifdef _WIN32
  PyMem_Free(path);
  PyMem_Free(skip);
#endif
#endif
  return result;
}

#ifdef _WIN32
static PyObject* posixfile(PyObject* self, PyObject* args, PyObject* kwds) {
  static char* kwlist[] = {"name", "mode", "buffering", NULL};
  PyObject* file_obj = NULL;
  PyObject* name_obj = NULL;
  char* name = NULL;
  char* mode = "rb";
  DWORD access = 0;
  DWORD creation;
  HANDLE handle;
  int fd, flags = 0;
  int bufsize = -1;
  char m0, m1, m2;
  char fpmode[4];
  int fppos = 0;
  int plus;
  FILE* fp;
  if (!PyArg_ParseTupleAndKeywords(
          args, kwds, "O|si:posixfile", kwlist, &name_obj, &mode, &bufsize))
    return NULL;

  m0 = mode[0];
  m1 = m0 ? mode[1] : '\0';
  m2 = m1 ? mode[2] : '\0';
  plus = m1 == '+' || m2 == '+';

  fpmode[fppos++] = m0;
  if (m1 == 'b' || m2 == 'b') {
    flags = _O_BINARY;
    fpmode[fppos++] = 'b';
  } else
    flags = _O_TEXT;
  if (m0 == 'r' && !plus) {
    flags |= _O_RDONLY;
    access = GENERIC_READ;
  } else {
    /*
    work around http://support.microsoft.com/kb/899149 and
    set _O_RDWR for 'w' and 'a', even if mode has no '+'
    */
    flags |= _O_RDWR;
    access = GENERIC_READ | GENERIC_WRITE;
    fpmode[fppos++] = '+';
  }
  fpmode[fppos++] = '\0';

  switch (m0) {
    case 'r':
      creation = OPEN_EXISTING;
      break;
    case 'w':
      creation = CREATE_ALWAYS;
      break;
    case 'a':
      creation = OPEN_ALWAYS;
      flags |= _O_APPEND;
      break;
    default:
      PyErr_Format(
          PyExc_ValueError,
          "mode string must begin with one of 'r', 'w', "
          "or 'a', not '%c'",
          m0);
      goto bail;
  }

#ifdef IS_PY3K
  wchar_t* wname;

  if (!PyUnicode_Check(name_obj)) {
    goto bail;
  }

  // This apparently encodes in utf-16. PyUnicode_AsUTF16String doesn't work
  // here for some reason.
  wname = PyUnicode_AsWideCharString(name_obj, NULL);
  if (!wname) {
    goto bail;
  }

  size_t name_max = wcslen(wname) * 4;
  name = PyMem_Malloc(name_max);
  wcstombs(name, wname, name_max);

  handle = CreateFileW(
      wname,
      access,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      NULL,
      creation,
      FILE_ATTRIBUTE_NORMAL,
      0);
  PyMem_Free(wname);
#else
  name = PyBytes_AsString(name_obj);
  handle = CreateFileA(
      name,
      access,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      NULL,
      creation,
      FILE_ATTRIBUTE_NORMAL,
      0);
#endif

  if (handle == INVALID_HANDLE_VALUE) {
    PyErr_SetFromWindowsErrWithFilename(GetLastError(), name);
    goto bail;
  }

  fd = _open_osfhandle((intptr_t)handle, flags);

  if (fd == -1) {
    CloseHandle(handle);
    PyErr_SetFromErrnoWithFilename(PyExc_IOError, name);
    goto bail;
  }
#ifndef IS_PY3K
  fp = _fdopen(fd, fpmode);
  if (fp == NULL) {
    _close(fd);
    PyErr_SetFromErrnoWithFilename(PyExc_IOError, name);
    goto bail;
  }

  file_obj = PyFile_FromFile(fp, name, mode, fclose);
  if (file_obj == NULL) {
    fclose(fp);
    goto bail;
  }

  PyFile_SetBufSize(file_obj, bufsize);
#else
  file_obj = PyFile_FromFd(fd, name, mode, bufsize, NULL, NULL, NULL, 1);
  if (file_obj == NULL)
    goto bail;
#endif
bail:
#ifdef IS_PY3K
  PyMem_Free(name);
#endif
  return file_obj;
}
#endif

static char osutil_doc[] = "Native operating system services.";

static PyMethodDef methods[] = {
    {"listdir",
     (PyCFunction)listdir,
     METH_VARARGS | METH_KEYWORDS,
     "list a directory\n"},
#ifdef _WIN32
    {"posixfile",
     (PyCFunction)posixfile,
     METH_VARARGS | METH_KEYWORDS,
     "Open a file with POSIX-like semantics.\n"
     "On error, this function may raise either a WindowsError or an IOError."},
#else
    {"statfiles",
     (PyCFunction)statfiles,
     METH_VARARGS | METH_KEYWORDS,
     "stat a series of files or symlinks\n"
     "Returns None for non-existent entries and entries of other types.\n"},
#ifdef CMSG_LEN
    {"recvfds",
     (PyCFunction)recvfds,
     METH_VARARGS,
     "receive list of file descriptors via socket\n"},
#endif
#ifndef SETPROCNAME_USE_NONE
    {"setprocname",
     (PyCFunction)setprocname,
     METH_VARARGS,
     "set process title (best-effort)\n"},
#endif
    {"unblocksignal",
     (PyCFunction)unblocksignal,
     METH_VARARGS,
     "change signal mask to unblock a given signal\n"},
#endif /* ndef _WIN32 */
    {NULL, NULL}};

static const int version = 2;

#ifdef IS_PY3K
static struct PyModuleDef osutil_module =
    {PyModuleDef_HEAD_INIT, "osutil", osutil_doc, -1, methods};

PyMODINIT_FUNC PyInit_osutil(void) {
  PyObject* m;
  if (PyType_Ready(&listdir_stat_type) < 0) {
    return NULL;
  }

  m = PyModule_Create(&osutil_module);
  PyModule_AddIntConstant(m, "version", version);
#ifndef _WIN32
  PyModule_AddIntConstant(m, "O_CLOEXEC", O_CLOEXEC);
#endif
  return m;
}
#else
PyMODINIT_FUNC initosutil(void) {
  PyObject* m;
  if (PyType_Ready(&listdir_stat_type) == -1)
    return;

  m = Py_InitModule3("osutil", methods, osutil_doc);
  PyModule_AddIntConstant(m, "version", version);
#ifndef _WIN32
  PyModule_AddIntConstant(m, "O_CLOEXEC", O_CLOEXEC);
#endif
}
#endif
