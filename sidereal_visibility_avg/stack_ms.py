from casacore.tables import table, taql
import numpy as np
from os import path, remove
import sys
import psutil
from glob import glob
from scipy.ndimage import gaussian_filter1d
import gc

from .utils.arrays_and_lists import find_closest_index_list
from .utils.file_handling import load_json, read_mapping
from .utils.ms_info import make_ant_pairs, get_data_arrays
from .utils.parallel import sum_arrays, add_into_new_data
from .utils.printing import print_progress_bar
from .utils.clean import clean_binary_file


class Stack:
    """
    Stack measurement sets in template empty.ms
    """
    def __init__(self, msin: list = None, outname: str = 'empty.ms', chunkmem: float = 1., tmp_folder: str = '.'):
        if not path.exists(outname):
            sys.exit(f"ERROR: Template {outname} has not been created or is deleted")
        print("\n\n==== Start stacking ====\n")
        self.template = table(outname, readonly=False, ack=False)
        self.mslist = msin
        self.outname = outname
        self.flag = False

        # Freq
        F = table(self.outname+'::SPECTRAL_WINDOW', ack=False)
        self.ref_freqs = F.getcol("CHAN_FREQ")[0]
        self.freq_len = self.ref_freqs.__len__()
        F.close()

        # Memory and chunk size
        self.num_cpus = psutil.cpu_count(logical=True)
        total_memory = psutil.virtual_memory().total / (1024 ** 3)  # in GB
        total_memory /= chunkmem
        self.chunk_size = min(int(total_memory * (1024 ** 3) / np.dtype(np.float128).itemsize/64/self.freq_len), 1_000_000)
        print(f"\n---------------\nChunk size ==> {self.chunk_size}")

        self.tmp_folder = tmp_folder
        if self.tmp_folder[-1]!='/':
            self.tmp_folder+='/'

    def smooth_uvw(self):
        """
        Smooth UVW values (EXPERIMENTAL, CURRENTLY NOT USED)
        """

        uvw, _ = get_data_arrays('UVW', self.T.nrows())
        uvw[:] = self.T.getcol("UVW")
        time = self.T.getcol("TIME")

        ants = table(self.outname + "::ANTENNA", ack=False)
        baselines = np.c_[make_ant_pairs(ants.nrows(), 1)]
        ants.close()

        print('Smooth UVW')
        for idx_b, baseline in enumerate(baselines):
            print_progress_bar(idx_b, len(baselines))
            idxs = []
            for baseline_json in glob(self.tmp_folder+f"*baseline_mapping/{baseline[0]}-{baseline[1]}.json"):
                idxs += list(load_json(baseline_json).values())
            sorted_indices = np.argsort(time[idxs])
            for i in range(3):
                uvw[np.array(idxs)[sorted_indices], i] = gaussian_filter1d(uvw[np.array(idxs)[sorted_indices], i], sigma=2)

        self.T.putcol('UVW', uvw)


    def stack_all(self, column: str = 'DATA', interpolate_uvw: bool = False, safe_mem: bool = False):
        """
        Stack all MS

        :param:
            - column: column name (currently only DATA)
            - interpolate_uvw: interpolate uvw coordinates (nearest neightbour + weighted average)
            - safe_mem: use always memmap for DATA and WEIGHT_SPECTRUM
        """

        if column == 'DATA':
            if interpolate_uvw:
                columns = ['UVW', column, 'WEIGHT_SPECTRUM']
            else:
                columns = [column, 'WEIGHT_SPECTRUM']
        else:
            sys.exit("ERROR: Only column 'DATA' allowed (for now)")

        # Get template data
        with table(path.abspath(self.outname), readonly=False, ack=False) as self.T:

            # Loop over columns
            for col in columns:

                gc.collect()

                if col == 'UVW':
                    new_data, uvw_weights = get_data_arrays(col, self.T.nrows(), self.freq_len, always_memmap=safe_mem, tmp_folder=self.tmp_folder)
                elif col=='WEIGHT_SPECTRUM':
                    new_data, _ = get_data_arrays(col, self.T.nrows(), self.freq_len, always_memmap=safe_mem, tmp_folder=self.tmp_folder)
                else:
                    new_data, _ = get_data_arrays(col, self.T.nrows(), self.freq_len, always_memmap=True, tmp_folder=self.tmp_folder)


                # Loop over measurement sets
                for ms in self.mslist:

                    print(f'\n{col} :: {ms}')

                    # Open MS table TODO: Make more efficient?
                    if col == 'DATA':
                        t = taql(f"SELECT {col} * WEIGHT_SPECTRUM AS DATA_WEIGHTED FROM {path.abspath(ms)}")
                    elif col == 'UVW':
                        t = taql(f"SELECT {col},WEIGHT_SPECTRUM FROM {path.abspath(ms)}")
                    else:
                        t = taql(f"SELECT {col} FROM {path.abspath(ms)}")

                    print("TAQL query finished")

                    # Get freqs offset
                    if col != 'UVW':
                        f = table(ms+'::SPECTRAL_WINDOW', ack=False)
                        freqs = f.getcol("CHAN_FREQ")[0]
                        freq_idxs = find_closest_index_list(freqs, self.ref_freqs)
                        f.close()

                    print('Collect relevant frequencies')

                    # Make antenna mapping in parallel
                    mapping_folder = self.tmp_folder + ms + '_baseline_mapping'

                    print('Read baseline mapping')
                    indices, ref_indices = read_mapping(mapping_folder)

                    if len(indices)==0:
                        sys.exit('ERROR: cannot find *_baseline_mapping folders')

                    # Chunked stacking!
                    chunks = len(indices)//self.chunk_size + 1
                    print(f'Stacking in {chunks} chunks')
                    for chunk_idx in range(chunks):
                        print_progress_bar(chunk_idx, chunks+1)
                        data = t.getcol(col+"_WEIGHTED" if col == 'DATA' else col,
                                                startrow=chunk_idx * self.chunk_size, nrow=self.chunk_size)

                        # Reduce to one polarisation, since weights have same values for other polarisations
                        if col=='WEIGHT_SPECTRUM':
                            data = data[..., 0]

                        row_idxs_new = ref_indices[chunk_idx * self.chunk_size:self.chunk_size * (chunk_idx+1)]
                        row_idxs = [int(i - chunk_idx * self.chunk_size) for i in
                                    indices[chunk_idx * self.chunk_size:self.chunk_size * (chunk_idx+1)]]


                        if col == 'UVW':
                            try:
                                weights = t.getcol("WEIGHT_SPECTRUM", startrow=chunk_idx * self.chunk_size, nrow=self.chunk_size)
                                weights = np.tile(weights[row_idxs, :, 0].mean(axis=1), 3).reshape(len(row_idxs), 3)
                            except IndexError:
                                print("Issues with UVW_weights, continue with ones")
                                weights = np.ones(data[row_idxs, :].shape)

                            subdata_new = new_data[row_idxs_new, :]
                            subdata = data[row_idxs, :]
                            result = sum_arrays(subdata_new, subdata* weights)
                            new_data[row_idxs_new, :] = result
                            result = sum_arrays(uvw_weights[row_idxs_new, :], weights)
                            uvw_weights[row_idxs_new, :] = result

                            try:
                                uvw_weights.flush()
                            except AttributeError:
                                pass

                        else:
                            subdata_new = new_data[np.ix_(row_idxs_new, freq_idxs)]
                            subdata = data[row_idxs, :]
                            idx_mask = np.ix_(row_idxs_new, freq_idxs)
                            new_data[idx_mask] = sum_arrays(subdata_new, subdata)
                            #TODO: Issues with following njit--nopython function
                            # add_into_new_data(new_data, data, row_idxs_new, row_idxs, freq_idxs)

                        # cleanup
                        del subdata
                        del subdata_new
                        del data
                        gc.collect()

                    try:
                        new_data.flush()
                    except AttributeError:
                        pass

                    print_progress_bar(chunk_idx, chunks)
                    t.close()

                print(f'Put column {col}')
                if col == 'UVW':
                    uvw_weights[uvw_weights == 0] = 1
                    new_data /= uvw_weights
                    new_data[new_data != new_data] = 0.

                if col == 'WEIGHT_SPECTRUM':
                    shape = list(new_data.shape)+[4]
                    new_data = np.tile(new_data, 4).reshape(shape)

                for chunk_idx in range(self.T.nrows() // self.chunk_size + 1):
                    start = chunk_idx * self.chunk_size
                    end = min(start + self.chunk_size, self.T.nrows())  # Ensure we don't overrun the total rows
                    self.T.putcol(col, new_data[start:end], startrow=start, nrow=end - start)

                # clean up
                clean_binary_file(self.tmp_folder + col.lower() + '.tmp.dat')


        # ADD FLAG
        print(f'Put column FLAG')
        taql(f'UPDATE {self.outname} SET FLAG = (WEIGHT_SPECTRUM == 0)')

        # NORM DATA
        print(f'Normalise column DATA')
        taql(f'UPDATE {self.outname} SET DATA = (DATA / WEIGHT_SPECTRUM) WHERE ANY(WEIGHT_SPECTRUM > 0)')

        print("----------\n")
