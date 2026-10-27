/* SPDX-License-Identifier: BSD-2-Clause-Patent */
/*
 * Register public libpthread 539.100.4 with XNU 12377's versioned pthread shims.
 *
 * XNU keeps the pthread implementation in a kext and panics in BSD initialization when nothing has
 * filled its function table: "pthread kernel extension not loaded (function table is NULL)". There
 * is no kext here, so the archive scripts/build-pthread-12377.py builds registers itself from a
 * STARTUP handler instead. build-pthread-12377.py compiles this file with the recorded compile
 * command of bsd/kern/pthread_shims.c, so it sees exactly the include set and flags that the shims
 * it registers against were built with.
 *
 * The xnu-10063 kernel does the same thing through patches/xnu/source-pthread-bootstrap.patch,
 * which adds the file to the work tree; this build keeps it out of the XNU sources.
 */
#include <kern/startup.h>
#include <kern/debug.h>
#include <libkern/section_keywords.h>
#include <mach/kmod.h>

extern kern_return_t pthread_start(kmod_info_t *, void *);
SECURITY_READ_ONLY_LATE(kern_return_t) source_pthread_status = KERN_FAILURE;

static void
source_pthread_bootstrap(void)
{
	static kmod_info_t info = {
		.info_version = KMOD_INFO_VERSION,
		.name = "source-libpthread",
		.version = "539.100.4",
	};
	source_pthread_status = pthread_start(&info, NULL);
	if (source_pthread_status != KERN_SUCCESS) {
		panic("source libpthread registration failed: %d", source_pthread_status);
	}
}
STARTUP(EARLY_BOOT, STARTUP_RANK_SECOND, source_pthread_bootstrap);
