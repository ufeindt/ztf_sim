from constants import *
from utils import approx_hours_of_darkness


class ObservingProgram(object):

    def __init__(self, program_id, subprogram_name,
                 observing_time_fraction, field_ids,
                 filter_ids, internight_gap, n_visits_per_night,
                 intranight_gap, intranight_half_width,
                 nightly_priority='oldest', filter_choice='rotate'):

        self.program_id = program_id
        self.subprogram_name = subprogram_name
        self.observing_time_fraction = observing_time_fraction
        self.field_ids = field_ids
        self.filter_ids = filter_ids

        self.internight_gap = internight_gap
        self.n_visits_per_night = n_visits_per_night
        self.intranight_gap = intranight_gap
        self.intranight_half_width = intranight_half_width

        self.nightly_priority = nightly_priority
        self.filter_choice = filter_choice

    def assign_nightly_requests(self, time, fields, block_programs=True,
                                **kwargs):

        # need a way to make this flexible without coding a new class for
        # every single way we could pick which fields to observe
        # some kind of config files?
        # or just instantiate with nightly_priority="", with
        # analagous functions to cadence.py?
        # selection functions:
        # nightly_priority='oldest":
        #    Prioritize fields for which it has been the longest
        #    time since the revisit
        # nightly_priority='radist':
        #    Prioritize fields closest to ra0
        # nightly_priority='decdist':
        #    Prioritize fields closest to dec0.  Give list to rotate???
        # nightly_priority='random'
        #    Choose fields at random

        # e.g.: fields assigned % n nights, rotate
        #               -> could handle this with cadence pars directly!
        #               but sensitive to state--get random initial set
        #               of fields  baked in
        #       observe oldest fields last observed
        #       observe fields on a fixed rotation cadence
        #       observe all fields
        #       observe fields nearest to an RA or Dec line
        #       observe randomly chosen fields

        # filters are given in filter_ids:
        # either a set of filters, or a fixed sequence
        # filter_choice = 'rotate':
        #   use one filter set per night, keyed by mjd % n_filters
        # filter_choice = 'sequence':
        #   use hard-coded sequence given in filter_ids

        # compute nightly altaz blocks and observability windows
        fields.compute_blocks(time)
        fields.compute_observability()

        n_filters = len(set(self.filter_ids))
        if self.filter_choice == 'rotate':
            night_index_filters = np.floor(time.mjd % n_filters).astype(np.int)
            filter_ids_tonight = self.filter_ids[night_index_filters]
            filter_ids_last_night = self.filter_ids[night_index_filters - 1]
        else:
            filter_ids_tonight = list(set(self.filter_ids))

        # how many fields can we observe tonight?
        n_requests = self.number_of_allowed_requests(time)

        n_fields = np.round(n_requests / self.n_visits_per_night)

        # maintain balance between programs
        if not block_programs:
            obs_count_by_program = fields.count_total_obs_by_program()
            total_obs = np.sum(obs_count_by_program.values())
            # difference in expected obs from allowed fraction
            delta = np.round(obs_count_by_program[self.program_id] -
                             self.observing_time_fraction * total_obs)

            # TODO: tweak as needed
            # how quickly do we want to take to reach equalization?
            CATCHUP_FACTOR = 0.20
            n_fields -= np.round(delta * CATCHUP_FACTOR).astype(np.int)

            if n_fields <= 0:
                # TODO: logging
                print('No fields requested for program {}'.format(program_id))
                return {}

        # Choose which fields will be observed

        obs_field_ids = fields.select_field_ids(
            # subtract an extra day since we are at the start of the night
            last_observed_range=[Time('2001-01-01').mjd,
                                 (time - self.internight_gap - 1 * u.day).mjd],
            program_id=self.program_id, filter_id=filter_ids_tonight,
            reducefunc=[np.min, np.min],  # we want oldest possible fields
            observable_hours_range=[self.n_visits_per_night * \
                                    self.intranight_gap.to(u.hour).value, 24.])

        # now form the intersection of observable fields and the OP fields
        pool_ids = obs_field_ids.intersection(self.field_ids)

        request_fields = fields.fields.loc[pool_ids]

        if n_fields > len(request_fields):
            # TODO: logging
            print('Not enough requests to fill available time!')

        # sort request sets by chosen priority metric

        # first calculate oldest observations
        last_obs_keys = ['last_observed_{}_{}'.format(self.program_id, k)
                         for k in np.atleast_1d(filter_ids_tonight)]
        # make a new joint column
        oldest_obs = request_fields[last_obs_keys].apply(np.min, axis=1)
        oldest_obs.name = 'oldest_obs'
        request_fields = request_fields.join(oldest_obs)

        if self.nightly_priority == 'oldest':
            # now grab the top n_fields sorted by last observed date
            request_fields = request_fields.sort_values(
                by='oldest_obs').iloc[:n_fields]

        elif self.nightly_priority == 'mean_observable_airmass':
            # now grab the top n_fields sorted by average airmass
            # above MAX_AIRMASS tonight.  (We already cut to only fields up
            # long enough to get n_visits_per_night)
            # TODO: make this a smarter, SNR-based calculation

            request_fields = request_fields.join(
                fields.mean_observable_airmass)

            request_fields = request_fields.sort_values(
                by='mean_observable_airmass').iloc[:n_fields]

        elif self.nightly_priority == 'rotate':
            field_rotation_nights = np.floor(len(request_fields) // n_fields)
            # nightly rotation by ra strips
            night_index_fields = np.floor(
                time.mjd % field_rotation_nights).astype(np.int)
            request_fields['ra_rotation_index'] = \
                np.floor(request_fields['ra'] %
                         field_rotation_nights).astype(np.int)
            # drop "off" strips
            wonstrip = request_fields[
                'ra_rotation_index'] == night_index_fields
            request_fields = request_fields[wonstrip]
            # and take fields with last observed date
            request_fields = request_fields.sort_values(
                by='oldest_obs').iloc[:n_fields]

        elif self.nightly_priority == 'radist':
            assert ('ra0' in kwargs)
            # TODO: do I want spherical distance rather than ra distance?
            raise NotImplementedError
        elif self.nightly_priority == 'decdist':
            assert ('dec0' in kwargs)
            raise NotImplementedError
        elif self.nightly_priority == 'random':
            request_fields = request_fields.reindex(
                np.random.permutation(request_fields.index))[:n_fields]
        else:
            raise ValueError('requested prioritization scheme not found')

        # construct request sets: list of inputs to RequestPool.add_requests
        # scalar everything except field_ids

        if self.filter_choice == 'rotate':
            filter_sequence = [filter_ids_tonight for i in
                               range(self.n_visits_per_night)]
        elif self.filter_choice == 'sequence':
            assert(len(self.filter_ids) == self.n_visits_per_night)
            filter_sequence = self.filter_ids

        request_set = []
        # first visit
        request_set.append(
            {'program_id': self.program_id,
             'subprogram_name': self.subprogram_name,
             'field_ids': request_fields.index,
             'filter_id': filter_sequence[0],
             'cadence_func': 'time_since_obs',
             'cadence_pars': {'ref_obs': 'last_observed',
                              'window_start': self.internight_gap.to(u.day).value,
                              # use a very large value here: gets added to
                              # last_obs.  remember that we reset each night
                              # anyway
                              'window_stop': (100 * u.year).to(u.day).value,
                              # TODO: do I want to specify this in some cases?
                              'prev_filter': 'any'},
             'request_number_tonight': 1,
             'total_requests_tonight': self.n_visits_per_night,
             'priority': 1})
        # additional visits
        for i in range(self.n_visits_per_night - 1):
            request_set.append(
                {'program_id': self.program_id,
                 'subprogram_name': self.subprogram_name,
                 'field_ids': request_fields.index,
                 'filter_id': filter_sequence[i + 1],
                 'cadence_func': 'time_since_obs',
                 #'cadence_pars': {'ref_obs': 'first_obs_tonight',
                 'cadence_pars': {'ref_obs': 'last_observed',
                                  'window_start': (self.intranight_gap).to(u.day).value - self.intranight_half_width.to(u.day).value,
                                  #'window_start': (i + 1) * (self.intranight_gap).to(u.day).value - self.intranight_half_width.to(u.day).value,
                                  # run the window the rest of the night
                                  'window_stop': 0.6,
                                  #'window_stop': (i + 1) * (self.intranight_gap).to(u.day).value + self.intranight_half_width.to(u.day).value,
                                  'prev_filter': filter_sequence[i]},
                 #'prev_filter': 'any'},
                 'request_number_tonight': i + 2,
                 'total_requests_tonight': self.n_visits_per_night,
                 'priority': 1})

        return request_set

    def number_of_allowed_requests(self, time):
        """ Count the (maximal) number of requests allowed for this program tonight."""

        # TODO: implement balancing of program observing time

        # "fudge factor" to provide ~15% extra requests for all programs
        # to minimize QueueEmptyErrors...
        # TODO: test how much we need this...
        FUDGE_FACTOR = 1.15

        obs_time = approx_hours_of_darkness(
            time) * self.observing_time_fraction

        n_requests = (obs_time.to(u.min) /
                      (EXPOSURE_TIME + READOUT_TIME).to(u.min)).value[0]  \
            * FUDGE_FACTOR
        return np.round(n_requests).astype(np.int)
