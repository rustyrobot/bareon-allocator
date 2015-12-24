#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See then
#    License for the specific language governing permissions and limitations
#    under the License.

import itertools
import six

from scipy.optimize import linprog
from scipy.ndimage.interpolation import shift

import numpy as np

from oslo_log import log


LOG = log.getLogger(__name__)


def shift(arr, steps, val=0):
    res_arr = np.roll(arr, steps)
    np.put(res_arr, range(steps), val)

    return res_arr


def grouper(iterable, n, fillvalue=None):
    """Collect data into fixed-length chunks or blocks
    Source: https://docs.python.org/2/library/itertools.html#recipes
    """
    args = [iter(iterable)] * n
    return itertools.izip_longest(fillvalue=fillvalue, *args)


class Disk(object):

    def __init__(self, **kwargs):
        for k, v in six.iteritems(kwargs):
            setattr(self, k, v)


class Space(object):

    def __init__(self, **kwargs):
        for k, v in six.iteritems(kwargs):
            setattr(self, k, v)


class DynamicAllocator(object):

    def __init__(self, hw_info, schema):
        self.disks = [Disk(**disk) for disk in  hw_info['disks']]
        self.spaces = [Space(**space) for space in schema]

        # Add fake volume Unallocated, in order to be able
        # to have only volumes with minimal size, without
        # additional space allocation
        self.lp = DynamicAllocationLinearProgram(self.disks, self.spaces)

    def generate_static(self):
        sizes = self.lp.solve()

        return sizes


class DynamicAllocationLinearProgram(object):
    """Use Linear Programming method [0] (the method itself has nothing to do
    with computer-programming) in order to formulate and solve the problem
    of spaces allocation on disks, with the best outcome.

    In this implementation scipy is being used since it already implements
    simplex algorithm to find the best feasible solution.

    [0] https://en.wikipedia.org/wiki/Linear_programming
    [1] http://docs.scipy.org/doc/scipy-0.16.0/reference/generated
                             /scipy.optimize.linprog.html
    [2] https://en.wikipedia.org/wiki/Simplex_algorithm
    """

    def __init__(self, disks, spaces):
        self.disks = disks
        self.spaces = spaces
        # Coefficients of the linear objective minimization function.
        # During iteration over vertexes the function is used to identify
        # if current solution (vertex) satisfies the equation more, than
        # previous one.
        # Example of equation: c[0]*x1 + c[1]*x2
        self.objective_function_coefficients = []

        # A matrix which, gives the values of the equality constraints at x,
        # when multipled by x.
        self.equality_constraint_matrix = []

        # An array of values representing right side of equation,
        # left side is represented by row of `equality_constraint_matrix`
        # matrix
        self.equality_constraint_vector = np.array([])

        self.upper_bound_constraint_matrix = []
        self.upper_bound_constraint_vector = []
        self.lower_bound_constraint_matrix = []
        self.lower_bound_constraint_vector = []

        # Specify boundaries of each x in the next format (min, max). Use
        # None for one of min or max when there is no bound.
        self.bounds = np.array([])

        # For each space, xn (size of the space) is represented
        # for each disk as separate variable, so for each
        # disk we have len(spaces) * len(disks) sizes
        self.x_amount = len(self.disks) * len(self.spaces)

        self._init_equation(self.disks, self.spaces)
        self._init_min_max()

    def solve(self):
        upper_bound_matrix = self._make_upper_bound_constraint_matrix() or None
        upper_bound_vector = self._make_upper_bound_constraint_vector() or None

        solution = linprog(
            self.objective_function_coefficients,
            A_eq=self.equality_constraint_matrix,
            b_eq=self.equality_constraint_vector,
            A_ub=upper_bound_matrix,
            b_ub=upper_bound_vector,
            bounds=self.bounds,
            options={"disp": False})

        LOG.debug("Solution: %s", solution)

        return self._convert_solution(solution)


    def _init_min_max(self):
        for space_idx, space in enumerate(self.spaces):
            row = self._make_matrix_row()
            max_size = getattr(space, 'max_size', None)
            min_size = getattr(space, 'min_size', None)

            for disk_idx in range(len(self.disks)):
                row[disk_idx * len(self.spaces) + space_idx] = 1

            if min_size is not None:
                self.lower_bound_constraint_matrix.append(row)
                self.lower_bound_constraint_vector.append(min_size)

            if max_size is not None:
                self.upper_bound_constraint_matrix.append(row)
                self.upper_bound_constraint_vector.append(max_size)

    def _make_matrix_row(self):
        return np.zeros(self.x_amount)

    def _make_upper_bound_constraint_matrix(self):
        """Upper bound constraint matrix consist of upper bound
        matrix and lower bound matrix wich changed sign
        """
        return (self.upper_bound_constraint_matrix +
                [[-i for i in row] for row in self.lower_bound_constraint_matrix])

    def _make_upper_bound_constraint_vector(self):
        """Upper bound constraint vector consist of upper bound
        and lower bound, with changed sign
        """
        return (self.upper_bound_constraint_vector +
                [-i for i in self.lower_bound_constraint_vector])

    def _convert_solution(self, solution):
        result = []

        spaces_grouped_by_disk = list(grouper(solution.x, len(self.spaces)))
        for disk_i in range(len(self.disks)):
            disk_id = self.disks[disk_i].id
            disk = {'disk_id': disk_id, 'size': self.disks[disk_i].size, 'spaces': []}
            spaces_for_disk = spaces_grouped_by_disk[disk_i]

            for space_i, space_size in enumerate(spaces_for_disk):
                disk['spaces'].append({
                    'space_id': self.spaces[space_i].id,
                    'size': space_size})

            result.append(disk)

        return result

    def _init_equation(self, disks, spaces):
        for d in disks:
            # Initialize constraints, each row in the matrix should
            # be equal to size of the disk
            self.equality_constraint_vector = np.append(self.equality_constraint_vector, d.size)

        # Initialize the matrix
        # In case of 2 spaces and 3 disks the result should be:
        # [[1, 1, 0, 0, 0, 0],
        #  [0, 0, 1, 1, 0, 0],
        #  [0, 0, 0, 0, 1, 1]]
        #
        # Explanation of the first row
        # [1, - x1 multiplier, size of space 1 on the first disk
        #  1, - x2 multiplier, size of space 2 on the first disk
        #  0, - x3 multiplier, size of space 1 on 2nd disk, 0 for the first
        #  0, - x4 multiplier, size of space 2 on 2nd disk, 0 for the first
        #  0, - x5 multiplier, size of space 1 on 3rd disk, 0 for the first
        #  0] - x6 multiplier, size of space 2 on 3rd disk, 0 for the first

        equality_matrix_row = self._make_matrix_row()

        # Amount of coefficients is equal to amount of columns in the matrix
        self._init_objective_function_coefficient(len(spaces) * len(disks))

        # Set first len(spaces) elements to 1
        equality_matrix_row = shift(equality_matrix_row, len(spaces), val=1)

        for _ in range(len(disks)):
            self.equality_constraint_matrix.append(equality_matrix_row)
            equality_matrix_row = shift(equality_matrix_row, len(spaces), val=0)

        # Size of each space should be more or equal to 0
        for _ in range(self.x_amount):
            self._add_bound(0, None)

    def _add_objective_function_coefficient(self):
        # By default the algorithm tries to minimize the solution
        # we should invert sign, in order to make it as a maximization
        # function, we want disks to be maximally allocated.
        # Coefficient for space per disk is 1, because all spaces
        # are equal and should not be adjusted.
        self.objective_function_coefficients.append(-1)

    def _init_objective_function_coefficient(self, size):
        self.objective_function_coefficients = [-1] * size

    def _add_bound(self, min_, max_):
        np.append(self.bounds, (min_, max_))