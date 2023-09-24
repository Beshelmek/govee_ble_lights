import array

def prepareMultiplePacketsData(protocol_type, header_array, data):
    result = []

    # Initialize the initial buffer
    header_length = len(header_array)
    initial_buffer = array.array('B', [0] * 20)
    initial_buffer[0] = protocol_type
    initial_buffer[1] = 0
    initial_buffer[2] = 1
    initial_buffer[4:4+header_length] = header_array

    # Create the additional buffer
    additional_buffer = array.array('B', [0] * 20)
    additional_buffer[0] = protocol_type
    additional_buffer[1] = 255

    remaining_space = 14 - header_length + 1

    if len(data) <= remaining_space:
        initial_buffer[header_length + 4:header_length + 4 + len(data)] = data
    else:
        excess = len(data) - remaining_space
        chunks = excess // 17
        remainder = excess % 17

        if remainder > 0:
            chunks += 1
        else:
            remainder = 17

        initial_buffer[header_length + 4:header_length + 4 + remaining_space] = data[0:remaining_space]
        current_index = remaining_space

        for i in range(1, chunks + 1):
            chunk = array.array('B', [0] * 17)
            chunk_size = remainder if i == chunks else 17
            chunk[0:chunk_size] = data[current_index:current_index + chunk_size]
            current_index += chunk_size

            if i == chunks:
                additional_buffer[2:2 + chunk_size] = chunk[0:chunk_size]
            else:
                chunk_buffer = array.array('B', [0] * 20)
                chunk_buffer[0] = protocol_type
                chunk_buffer[1] = i
                chunk_buffer[2:2+chunk_size] = chunk
                chunk_buffer[19] = sign_payload(chunk_buffer[0:19])
                result.append(chunk_buffer)

    initial_buffer[3] = len(result) + 2
    initial_buffer[19] = sign_payload(initial_buffer[0:19])
    result.insert(0, initial_buffer)

    additional_buffer[19] = sign_payload(additional_buffer[0:19])
    result.append(additional_buffer)

    return result

def sign_payload(data):
    checksum = 0
    for b in data:
        checksum ^= b
    return checksum & 0xFF
