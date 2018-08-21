from __future__ import absolute_import

import re
from collections import namedtuple, defaultdict
from datetime import timedelta

from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from django.utils import timezone

from sentry.api.bases import OrganizationEndpoint, EnvironmentMixin
from sentry.api.exceptions import ResourceDoesNotExist
from sentry.models import (
    Project, ProjectStatus, OrganizationMemberTeam,
    Environment,
)
from sentry.api.serializers.snuba import (
    SnubaResultSerializer, SnubaTSResultSerializer, value_from_row,
    SnubaLookup,
)
from sentry.utils import snuba


SnubaResultSet = namedtuple('SnubaResultSet', ('current', 'previous'))
SnubaTSResult = namedtuple('SnubaTSResult', ('data', 'start', 'end', 'rollup'))


def parse_stats_period(period):
    """
    Convert a value such as 1h into a
    proper timedelta.
    """
    m = re.match('^(\d+)([hdms]?)$', period)
    if not m:
        return None
    value, unit = m.groups()
    value = int(value)
    if not unit:
        unit = 's'
    return timedelta(**{
        {'h': 'hours', 'd': 'days', 'm': 'minutes', 's': 'seconds'}[unit]: value,
    })


def query(**kwargs):
    kwargs['referrer'] = 'health'
    kwargs['totals'] = True
    return snuba.raw_query(**kwargs)


class OrganizationHealthEndpointBase(OrganizationEndpoint, EnvironmentMixin):
    def empty(self):
        return Response({'data': []})

    def get_project_ids(self, request, organization):
        project_ids = set(map(int, request.GET.getlist('project')))

        before = project_ids.copy()
        if request.user.is_superuser:
            # Superusers can query any projects within the organization
            qs = Project.objects.filter(
                organization=organization,
                status=ProjectStatus.VISIBLE,
            )
        else:
            # Anyone else needs membership of the project
            qs = Project.objects.filter(
                organization=organization,
                teams__in=OrganizationMemberTeam.objects.filter(
                    organizationmember__user=request.user,
                    organizationmember__organization=organization,
                ).values_list('team'),
                status=ProjectStatus.VISIBLE,
            )

        # If no project's are passed through querystring, we want to
        # return all projects, otherwise, limit to the passed in ones
        if project_ids:
            qs = qs.filter(id__in=project_ids)

        project_ids = set(qs.values_list('id', flat=True))

        if before and project_ids != before:
            raise PermissionDenied

        if not project_ids:
            return

        # Make sure project_ids is now a list, otherwise
        # snuba isn't happy with it being a set
        return list(project_ids)

    def get_environment(self, request, organization):
        try:
            environment = self._get_environment_from_request(
                request,
                organization.id,
            )
        except Environment.DoesNotExist:
            raise ResourceDoesNotExist

        if environment is None:
            return []
        if environment.name == '':
            return [['tags[environment]', 'IS NULL', None]]
        return [['tags[environment]', '=', environment.name]]

    def get_query_condition(self, request, organization):
        qs = request.GET.getlist('q')
        if not qs:
            return [[]]

        conditions = defaultdict(list)
        for q in qs:
            try:
                tag, value = q.split(':', 1)
            except ValueError:
                # Malformed query
                continue

            try:
                lookup = SnubaLookup.get(tag)
            except KeyError:
                # Not a valid lookup tag
                continue

            conditions[lookup.tagkey].append(value)

        return [[k, 'IN', v] for k, v in conditions.items()]


class OrganizationHealthTopEndpoint(OrganizationHealthEndpointBase):
    MIN_STATS_PERIOD = timedelta(hours=1)
    MAX_STATS_PERIOD = timedelta(days=45)
    MAX_LIMIT = 50

    def get(self, request, organization):
        try:
            lookup = SnubaLookup.get(request.GET['tag'])
        except KeyError:
            raise ResourceDoesNotExist

        stats_period = parse_stats_period(request.GET.get('statsPeriod', '24h'))
        if stats_period is None or stats_period < self.MIN_STATS_PERIOD or stats_period >= self.MAX_STATS_PERIOD:
            return Response({'detail': 'Invalid statsPeriod'}, status=400)

        limit = int(request.GET.get('limit', '5'))
        if limit > self.MAX_LIMIT:
            return Response({'detail': 'Invalid limit: max %d' % self.MAX_LIMIT}, status=400)
        if limit <= 0:
            return self.empty()

        project_ids = self.get_project_ids(request, organization)
        if not project_ids:
            return self.empty()

        environment = self.get_environment(request, organization)
        query_condition = self.get_query_condition(request, organization)

        aggregations = [('count()', '', 'count')]
        if 'topk' in request.GET:
            topk = int(request.GET['topk'])
            aggregations += [
                ('topK(%d)' % topk, 'project_id', 'top_projects'),
                ('uniq', 'project_id', 'total_projects'),
            ]

        now = timezone.now()

        data = query(
            end=now,
            start=now - stats_period,
            selected_columns=lookup.selected_columns,
            aggregations=aggregations,
            filter_keys={
                'project_id': project_ids,
            },
            conditions=lookup.conditions + query_condition + environment,
            groupby=lookup.columns,
            orderby='-count',
            limit=limit,
        )

        if not data['data']:
            return self.empty()

        values = []
        is_null = False
        for row in data['data']:
            value = value_from_row(row, lookup.columns)[0]
            if value is None:
                is_null = True
            else:
                values.append(value)

        previous = query(
            end=now - stats_period,
            start=now - (stats_period * 2),
            selected_columns=lookup.selected_columns,
            aggregations=[
                ('count()', '', 'count'),
            ],
            filter_keys={
                'project_id': project_ids,
            },
            conditions=lookup.conditions + query_condition + environment + [
                # This isn't really right, and is relying on the fact
                # that project_id is the other key in this composite key
                [lookup.tagkey, 'IN', values] if values else [],
                [lookup.tagkey, 'IS NULL', None] if is_null else [],
            ],
            groupby=lookup.columns,
        )

        serializer = SnubaResultSerializer(organization, lookup, request.user)
        return Response(
            serializer.serialize(
                SnubaResultSet(data, previous),
            ),
            status=200,
        )


class OrganizationHealthGraphEndpoint(OrganizationHealthEndpointBase):
    MIN_STATS_PERIOD = timedelta(hours=1)
    MAX_STATS_PERIOD = timedelta(days=90)

    def get(self, request, organization):
        try:
            lookup = SnubaLookup.get(request.GET['tag'])
        except KeyError:
            raise ResourceDoesNotExist

        stats_period = parse_stats_period(request.GET.get('statsPeriod', '24h'))
        if stats_period is None or stats_period < self.MIN_STATS_PERIOD or stats_period >= self.MAX_STATS_PERIOD:
            return Response({'detail': 'Invalid statsPeriod'}, status=400)

        interval = parse_stats_period(request.GET.get('interval', '1h'))
        if interval is None:
            interval = timedelta(hours=1)

        project_ids = self.get_project_ids(request, organization)
        if not project_ids:
            return self.empty()

        environment = self.get_environment(request, organization)
        query_condition = self.get_query_condition(request, organization)

        end = timezone.now()
        start = end - stats_period
        rollup = int(interval.total_seconds())

        data = query(
            end=end,
            start=start,
            rollup=rollup,
            selected_columns=lookup.selected_columns,
            aggregations=[
                ('count()', '', 'count'),
            ],
            filter_keys={
                'project_id': project_ids,
            },
            conditions=lookup.conditions + query_condition + environment,
            groupby=['time'] + lookup.columns,
            orderby='time',
        )

        serializer = SnubaTSResultSerializer(organization, lookup, request.user)
        return Response(
            serializer.serialize(
                SnubaTSResult(data, start, end, rollup),
            ),
            status=200,
        )


# TODO: I dunno if we'll need this ever

# class OrganizationHealthGraphEndpoint(OrganizationHealthEndpointBase):
#     MIN_STATS_PERIOD = timedelta(hours=1)
#     MAX_STATS_PERIOD = timedelta(days=90)

#     def get(self, request, organization):
#         try:
#             lookup = SnubaLookup.get(request.GET['tag'])
#         except KeyError:
#             raise ResourceDoesNotExist

#         stats_period = parse_stats_period(request.GET.get('statsPeriod', '24h'))
#         if stats_period is None or stats_period < self.MIN_STATS_PERIOD or stats_period >= self.MAX_STATS_PERIOD:
#             return Response({'detail': 'Invalid statsPeriod'}, status=400)

#         interval = parse_stats_period(request.GET.get('interval', '1h'))
#         if interval is None:
#             interval = timedelta(hours=1)

#         project_ids = self.get_project_ids(request, organization)
#         if not project_ids:
#             return self.empty()

#         environment = self.get_environment(request, organization)

#         now = timezone.now()

#         data = query(
#             end=now,
#             start=now - stats_period,
#             rollup=interval.total_seconds(),
#             selected_columns=lookup.selected_columns,
#             aggregations=[
#                 ('uniq', lookup.tagkey, 'count'),
#             ],
#             filter_keys={
#                 'project_id': project_ids,
#             },
#             conditions=[
#                 lookup.conditions,
#                 environment,
#             ],
#             groupby=['time'],
#             orderby='time',
#         )
#         # TODO: Use proper serializer
#         return Response({'data': data})
