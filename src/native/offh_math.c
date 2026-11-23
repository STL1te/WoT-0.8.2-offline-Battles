/*
 * offh_math.c - pure-math helpers for the WoT 0.8.2 offline-battles mod.
 *
 * Design constraints (deliberate):
 *   - No Python headers, no BigWorld, no CRT exceptions, no global mutable
 *     state.  The module is loaded through ctypes from the embedded
 *     Python 2.6 client, and ctypes releases the GIL for the duration of
 *     a foreign call, so the worker threads below are real parallelism.
 *   - Flat structure-of-arrays inputs.  The caller keeps one reusable
 *     buffer set per battle; nothing is allocated per call.
 *   - Deterministic output: identical inputs must produce identical
 *     candidate order, because the Python side compares C results against
 *     its own reference implementation while validating the port.
 *
 * Ported logic: gui/mods/offhangar/offline_battle.py
 *   _offh_ai_refresh_contacts() -> observer pre-filter (distance loop,
 *   stable distance sort, spotting.foliage_visibility_bound short-circuit).
 * Engine work (wg_collideSegment LOS, prebaked foliage queries) stays in
 * Python: this file only classifies pairs and never touches the world.
 *
 * Build (Windows client, 32-bit):
 *   i686-w64-mingw32-gcc -O2 -shared -static-libgcc -o offh_math.dll offh_math.c
 * Build (host benchmark):
 *   gcc -O2 -shared -fPIC -pthread -o offh_math.so offh_math.c
 */

#include <math.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <pthread.h>
#include <unistd.h>
#endif

#define OFFH_MATH_VERSION 1

/* Canonical 0.8.2 spotting constants; must match spotting.py.
 * All classification arithmetic is done in double so that a pair sitting on a
 * range boundary is decided exactly like the Python reference, which computes
 * in double.  Only the reported distance is narrowed back to float. */
#define OFFH_PROXIMITY_SPOT 50.0
#define OFFH_MAX_SPOT_DIST  500.0
/* Hard 500 m cutoff used by the contact pre-filter (distance_sq > 250000). */
#define OFFH_CUTOFF_DIST_SQ 250000.0
/* Upper bound on entities handled without heap allocation. */
#define OFFH_MAX_ENTITIES 256

/* Candidate classification returned to Python. */
#define OFFH_FLAG_SKIP      0   /* never emitted */
#define OFFH_FLAG_CANDIDATE 1   /* proximity spot, or foliage bound == True */
#define OFFH_FLAG_NEEDS_QUERY 2 /* foliage bound == None: Python must query */

/* Candidate order is capped so callers can size output buffers statically. */
#define OFFH_MAX_CANDIDATES 64

static double offh_clampd(double value, double minimum, double maximum)
{
    if (value < minimum) return minimum;
    if (value > maximum) return maximum;
    return value;
}

/* spotting.detection_distance(): 50 m floor, 500 m ceiling, linear camo. */
static double offh_detection_distance(double view_range, double camouflage)
{
    double vr = view_range > OFFH_PROXIMITY_SPOT ? view_range
                                                 : OFFH_PROXIMITY_SPOT;
    double camo = offh_clampd(camouflage, 0.0, 0.95);
    double distance = vr - (vr - OFFH_PROXIMITY_SPOT) * camo;
    return offh_clampd(distance, OFFH_PROXIMITY_SPOT, OFFH_MAX_SPOT_DIST);
}

/* spotting.foliage_visibility_bound(): True / False / -1 for "needs query". */
static int offh_foliage_bound(double distance_sq, double view_range,
                              double camouflage, double max_foliage_bonus)
{
    double clear_range;
    double worst_camouflage;
    double worst_range;

    if (distance_sq < 0.0) distance_sq = 0.0;
    clear_range = offh_detection_distance(view_range, camouflage);
    if (distance_sq > clear_range * clear_range) return 0;      /* False */
    worst_camouflage = offh_clampd(camouflage +
                                   (max_foliage_bonus > 0.0 ? max_foliage_bonus
                                                            : 0.0),
                                   0.0, 0.95);
    worst_range = offh_detection_distance(view_range, worst_camouflage);
    if (distance_sq <= worst_range * worst_range) return 1;     /* True */
    return -1;                                                  /* None */
}

typedef struct {
    /* inputs */
    const float *pos;             /* n * 3 (x, y, z) */
    const signed char *team;      /* n */
    const unsigned char *alive;   /* n */
    const float *view_range;      /* n */
    int entity_count;
    const int *targets;           /* target_count entity indices */
    const float *target_camo;     /* target_count */
    int target_count;
    float proximity_sq;           /* PROXIMITY_SPOT_DISTANCE ** 2 */
    float max_foliage_bonus;      /* 0.60 in the caller */
    /* outputs, laid out [target * OFFH_MAX_CANDIDATES + k] */
    int *out_idx;
    float *out_d2;
    unsigned char *out_flag;
    int *out_count;
    double proximity_sq_d;
    double max_foliage_bonus_d;
} offh_contact_job;

/* Stable insertion sort of the first `count` pairs by (d2, entity index).
 * The Python side uses list.sort(key=d2), which is stable, so equal
 * distances must keep ascending entity order to match exactly. */
static void offh_sort_pairs(double *d2, int *idx, int count)
{
    int i;
    for (i = 1; i < count; ++i) {
        double key_d2 = d2[i];
        int key_idx = idx[i];
        int j = i - 1;
        while (j >= 0 && (d2[j] > key_d2 ||
                          (d2[j] == key_d2 && idx[j] > key_idx))) {
            d2[j + 1] = d2[j];
            idx[j + 1] = idx[j];
            --j;
        }
        d2[j + 1] = key_d2;
        idx[j + 1] = key_idx;
    }
}

/* Compute the candidate list for one target.  Returns count, or -1 on
 * invalid arguments.  Scratch arrays live on the caller's stack. */
static int offh_one_target(const offh_contact_job *job, int slot,
                           double *scratch_d2, int *scratch_idx)
{
    int target = job->targets[slot];
    double camo;
    signed char observing_team;
    double tx, tz;
    int collected = 0;
    int emitted = 0;
    int base = slot * OFFH_MAX_CANDIDATES;
    int i;

    if (target < 0 || target >= job->entity_count) return -1;
    camo = job->target_camo[slot];
    observing_team = (job->team[target] == 1) ? 2 : 1;
    tx = job->pos[target * 3 + 0];
    tz = job->pos[target * 3 + 2];

    for (i = 0; i < job->entity_count; ++i) {
        double dx, dz, d2;
        if (!job->alive[i]) continue;
        if (job->team[i] != observing_team) continue;
        dx = tx - job->pos[i * 3 + 0];
        dz = tz - job->pos[i * 3 + 2];
        d2 = dx * dx + dz * dz;
        if (d2 > OFFH_CUTOFF_DIST_SQ) continue;   /* 500 m cutoff */
        if (collected >= OFFH_MAX_ENTITIES) break;   /* scratch is full */
        scratch_d2[collected] = d2;
        scratch_idx[collected] = i;
        ++collected;
    }

    offh_sort_pairs(scratch_d2, scratch_idx, collected);

    for (i = 0; i < collected && emitted < OFFH_MAX_CANDIDATES; ++i) {
        double d2 = scratch_d2[i];
        int observer = scratch_idx[i];
        int bound;
        unsigned char flag;

        if (d2 <= job->proximity_sq_d) {
            flag = OFFH_FLAG_CANDIDATE;
        } else {
            bound = offh_foliage_bound(d2, job->view_range[observer], camo,
                                       job->max_foliage_bonus_d);
            if (bound == 0) continue;
            flag = (bound == 1) ? OFFH_FLAG_CANDIDATE : OFFH_FLAG_NEEDS_QUERY;
        }
        job->out_idx[base + emitted] = observer;
        job->out_d2[base + emitted] = (float)d2;
        job->out_flag[base + emitted] = flag;
        ++emitted;
    }

    job->out_count[slot] = emitted;
    return emitted;
}

typedef struct {
    const offh_contact_job *job;
    int first_slot;
    int slot_count;
    int result;                   /* sum of counts, or -1 on error */
} offh_thread_arg;

static void offh_run_slots(offh_thread_arg *arg)
{
    double scratch_d2[OFFH_MAX_ENTITIES];
    int scratch_idx[OFFH_MAX_ENTITIES];
    int total = 0;
    int k;

    for (k = 0; k < arg->slot_count; ++k) {
        int count = offh_one_target(arg->job, arg->first_slot + k,
                                    scratch_d2, scratch_idx);
        if (count < 0) { arg->result = -1; return; }
        total += count;
    }
    arg->result = total;
}

#ifdef _WIN32
static DWORD WINAPI offh_thread_proc(LPVOID param)
{
    offh_run_slots((offh_thread_arg *)param);
    return 0;
}
#else
static void *offh_thread_proc(void *param)
{
    offh_run_slots((offh_thread_arg *)param);
    return NULL;
}
#endif

int offh_math_version(void)
{
    return OFFH_MATH_VERSION;
}

int offh_max_entities(void)
{
    return OFFH_MAX_ENTITIES;
}

int offh_max_candidates(void)
{
    return OFFH_MAX_CANDIDATES;
}

/*
 * Single-target entry point (also used to validate the multithreaded path).
 */
int offh_contact_candidates(const float *pos, const signed char *team,
                            const unsigned char *alive,
                            const float *view_range, int entity_count,
                            int target, float target_camo,
                            float proximity_distance,
                            float max_foliage_bonus,
                            int *out_idx, float *out_d2,
                            unsigned char *out_flag)
{
    offh_contact_job job;
    int targets[1];
    float camos[1];
    int count = 0;

    if (!pos || !team || !alive || !view_range || !out_idx || !out_d2 ||
        !out_flag)
        return -1;
    if (entity_count <= 0 || entity_count > OFFH_MAX_ENTITIES) return -2;

    targets[0] = target;
    camos[0] = target_camo;
    job.pos = pos;
    job.team = team;
    job.alive = alive;
    job.view_range = view_range;
    job.entity_count = entity_count;
    job.targets = targets;
    job.target_camo = camos;
    job.target_count = 1;
    job.proximity_sq = proximity_distance * proximity_distance;
    job.max_foliage_bonus = max_foliage_bonus;
    job.proximity_sq_d = (double)proximity_distance * (double)proximity_distance;
    job.max_foliage_bonus_d = max_foliage_bonus;
    job.out_idx = out_idx;
    job.out_d2 = out_d2;
    job.out_flag = out_flag;
    job.out_count = &count;

    {
        offh_thread_arg arg;
        arg.job = &job;
        arg.first_slot = 0;
        arg.slot_count = 1;
        arg.result = 0;
        offh_run_slots(&arg);
        if (arg.result < 0) return arg.result;
    }
    return count;
}

/*
 * Multi-target entry point: `target_count` independent candidate lists are
 * spread over `thread_count` OS threads.  Output buffers are indexed as
 * [slot * offh_max_candidates() + k].  Returns the total number of emitted
 * candidates, or a negative error code.
 */
int offh_contact_candidates_mt(const float *pos, const signed char *team,
                               const unsigned char *alive,
                               const float *view_range, int entity_count,
                               const int *targets, const float *target_camo,
                               int target_count, int thread_count,
                               float proximity_distance,
                               float max_foliage_bonus,
                               int *out_idx, float *out_d2,
                               unsigned char *out_flag, int *out_count)
{
    offh_contact_job job;
    int max_threads;
    int total = 0;

    if (!pos || !team || !alive || !view_range || !targets || !target_camo ||
        !out_idx || !out_d2 || !out_flag || !out_count)
        return -1;
    if (entity_count <= 0 || entity_count > OFFH_MAX_ENTITIES) return -2;
    if (target_count <= 0 || target_count > OFFH_MAX_ENTITIES) return -3;
    if (thread_count < 1) thread_count = 1;
    if (thread_count > target_count) thread_count = target_count;
    max_threads = thread_count > 16 ? 16 : thread_count;

    job.pos = pos;
    job.team = team;
    job.alive = alive;
    job.view_range = view_range;
    job.entity_count = entity_count;
    job.targets = targets;
    job.target_camo = target_camo;
    job.target_count = target_count;
    job.proximity_sq = proximity_distance * proximity_distance;
    job.max_foliage_bonus = max_foliage_bonus;
    job.proximity_sq_d = (double)proximity_distance * (double)proximity_distance;
    job.max_foliage_bonus_d = max_foliage_bonus;
    job.out_idx = out_idx;
    job.out_d2 = out_d2;
    job.out_flag = out_flag;
    job.out_count = out_count;

    if (max_threads <= 1) {
        offh_thread_arg arg;
        arg.job = &job;
        arg.first_slot = 0;
        arg.slot_count = target_count;
        arg.result = 0;
        offh_run_slots(&arg);
        return arg.result;
    }

    {
        offh_thread_arg args[16];
#ifdef _WIN32
        HANDLE handles[16];
#else
        pthread_t handles[16];
#endif
        int started = 0;
        int slot = 0;
        int t;
        int base_slots = target_count / max_threads;
        int extra = target_count % max_threads;

        for (t = 0; t < max_threads; ++t) {
            int slots = base_slots + (t < extra ? 1 : 0);
            if (slots <= 0) continue;
            args[t].job = &job;
            args[t].first_slot = slot;
            args[t].slot_count = slots;
            args[t].result = 0;
            slot += slots;
#ifdef _WIN32
            handles[t] = CreateThread(NULL, 0, offh_thread_proc, &args[t],
                                      0, NULL);
            if (handles[t] == NULL) { args[t].result = -1; continue; }
#else
            if (pthread_create(&handles[t], NULL, offh_thread_proc,
                               &args[t]) != 0) {
                handles[t] = 0;
                args[t].result = -1;
                continue;
            }
#endif
            ++started;
        }

        for (t = 0; t < max_threads; ++t) {
            if (args[t].slot_count <= 0) continue;
#ifdef _WIN32
            if (handles[t] != NULL) {
                WaitForSingleObject(handles[t], INFINITE);
                CloseHandle(handles[t]);
            } else {
                offh_run_slots(&args[t]);
            }
#else
            if (handles[t] != 0) pthread_join(handles[t], NULL);
            else offh_run_slots(&args[t]);
#endif
            if (args[t].result < 0) return -4;
            total += args[t].result;
        }
        (void)started;
    }
    return total;
}

/* ------------------------------------------------------------------ *
 * Self-test: does native code actually run on several cores inside the
 * client process while ctypes holds the GIL released?
 *
 * The workload is deliberately engine-free and deterministic: a fixed
 * number of sqrt() iterations per thread.  Python compares the serial
 * time against the parallel time and logs the ratio.  A ratio near 1
 * means the process is pinned to one core (or the OS refuses to spread
 * us), in which case every multithreading plan is worthless and must be
 * abandoned before any gameplay code depends on it.
 * ------------------------------------------------------------------ */

#ifdef _WIN32
static double offh_now_ms(void)
{
    LARGE_INTEGER counter;
    LARGE_INTEGER frequency;
    static double scale = 0.0;
    if (scale == 0.0) {
        if (!QueryPerformanceFrequency(&frequency) || !frequency.QuadPart)
            return (double)GetTickCount();
        scale = 1000.0 / (double)frequency.QuadPart;
    }
    if (!QueryPerformanceCounter(&counter)) return (double)GetTickCount();
    return (double)counter.QuadPart * scale;
}
#else
#include <time.h>
static double offh_now_ms(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0.0;
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1000000.0;
}
#endif

static volatile double offh_sink = 0.0;

typedef struct {
    int iterations;
    double result;
} offh_selftest_arg;

static void offh_selftest_work(offh_selftest_arg *arg)
{
    double acc = 0.0;
    int i;
    for (i = 1; i <= arg->iterations; ++i) {
        acc += sqrt((double)i) * 1.0000001;
        acc = sqrt(acc * acc + 1.0);
    }
    arg->result = acc;
}

#ifdef _WIN32
static DWORD WINAPI offh_selftest_proc(LPVOID param)
{
    offh_selftest_work((offh_selftest_arg *)param);
    return 0;
}
#else
static void *offh_selftest_proc(void *param)
{
    offh_selftest_work((offh_selftest_arg *)param);
    return NULL;
}
#endif

/* One thread, `iterations` of work.  Returns elapsed milliseconds. */
double offh_selftest_serial(int iterations)
{
    offh_selftest_arg arg;
    double t0;
    if (iterations <= 0) return -1.0;
    arg.iterations = iterations;
    arg.result = 0.0;
    t0 = offh_now_ms();
    offh_selftest_work(&arg);
    offh_sink = arg.result;
    return offh_now_ms() - t0;
}

/* `thread_count` threads, each doing `iterations`.  Returns elapsed
 * milliseconds of wall time, or a negative error code.  The same total
 * amount of work as offh_selftest_serial(thread_count * iterations). */
double offh_selftest_parallel(int thread_count, int iterations)
{
    offh_selftest_arg args[64];
#ifdef _WIN32
    HANDLE handles[64];
#else
    pthread_t handles[64];
#endif
    double t0;
    double total_ms;
    int t;

    if (thread_count <= 0 || thread_count > 64 || iterations <= 0) return -1.0;
    memset(args, 0, sizeof(args));
    memset(handles, 0, sizeof(handles));

    t0 = offh_now_ms();
    for (t = 0; t < thread_count; ++t) {
        args[t].iterations = iterations;
#ifdef _WIN32
        handles[t] = CreateThread(NULL, 0, offh_selftest_proc, &args[t], 0, NULL);
        if (handles[t] == NULL) {
            offh_selftest_work(&args[t]);
        }
#else
        if (pthread_create(&handles[t], NULL, offh_selftest_proc, &args[t]) != 0) {
            handles[t] = 0;
            offh_selftest_work(&args[t]);
        }
#endif
    }
    for (t = 0; t < thread_count; ++t) {
#ifdef _WIN32
        if (handles[t] != NULL) {
            WaitForSingleObject(handles[t], INFINITE);
            CloseHandle(handles[t]);
        }
#else
        if (handles[t] != 0) pthread_join(handles[t], NULL);
#endif
        offh_sink += args[t].result;
    }
    total_ms = offh_now_ms() - t0;
    return total_ms;
}

/* Number of logical CPUs visible to the process (0 when unknown). */
int offh_cpu_count(void)
{
#ifdef _WIN32
    SYSTEM_INFO info;
    memset(&info, 0, sizeof(info));
    GetSystemInfo(&info);
    return (int)info.dwNumberOfProcessors;
#else
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n > 0 ? (int)n : 0;
#endif
}

/* Processor affinity mask of the current process, or 0 when unavailable.
 * A mask with a single bit set explains a parallel speedup of 1.0. */
unsigned long long offh_affinity_mask(void)
{
#ifdef _WIN32
    DWORD_PTR process_mask = 0;
    DWORD_PTR system_mask = 0;
    if (!GetProcessAffinityMask(GetCurrentProcess(), &process_mask,
                                &system_mask))
        return 0ULL;
    return (unsigned long long)process_mask;
#else
    return 0ULL;
#endif
}
